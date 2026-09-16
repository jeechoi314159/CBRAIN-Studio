"""Run the actual C driver against a two-command-latency SPI chip model."""
import subprocess
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def driver(tmp_path_factory):
    root=tmp_path_factory.mktemp('rhd-driver')
    headers={
        'zephyr/kernel.h': '#include <stdint.h>\n#include <stdbool.h>\nvoid k_msleep(int);\nvoid k_busy_wait(int);\n',
        'zephyr/sys/printk.h': '#define printk(...) ((void)0)\n',
        'zephyr/sys/util.h': '#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))\n',
        'zephyr/drivers/spi.h': '''#include <stddef.h>
struct spi_dt_spec { int unused; };
struct spi_buf { void *buf; size_t len; };
struct spi_buf_set { const struct spi_buf *buffers; size_t count; };
#define SPI_DT_SPEC_GET(...) {0}
int spi_is_ready_dt(const struct spi_dt_spec *);
int spi_transceive_dt(const struct spi_dt_spec *, const struct spi_buf_set *, const struct spi_buf_set *);
'''}
    for name,content in headers.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
    harness=root/'harness.c'
    harness.write_text(r'''
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "zephyr/drivers/spi.h"
#include "cb_rhd.h"
#include "cb_proto.h"
static int mode, calls, attempts, calibration, wait_us, fail_sample;
static uint8_t regs[64];
static uint16_t pipeline[2];
void k_msleep(int ms) { assert(ms==50); attempts++; }
void k_busy_wait(int us) { wait_us+=us; }
int spi_is_ready_dt(const struct spi_dt_spec *d) { return mode!=1; }
int spi_transceive_dt(const struct spi_dt_spec *d, const struct spi_buf_set *tx, const struct spi_buf_set *rx) {
    calls++;
    if(mode==2 || fail_sample) return -5;
    assert(tx->count==1 && rx->count==1 && tx->buffers[0].len==2 && rx->buffers[0].len==2);
    uint8_t *a=tx->buffers[0].buf, *b=rx->buffers[0].buf;
    uint16_t cmd=(a[0]<<8)|a[1], value=0;
    uint8_t reg=(cmd>>8)&63;
    if((cmd&0xc000)==0x8000) { if(reg<18 && !(mode==5 && reg==4)) regs[reg]=cmd&255; value=0xff00|(cmd&255); }
    else if((cmd&0xc000)==0xc000) value=regs[reg];
    else if(cmd==0x5500) { calibration++; assert(wait_us>=100); }
    else if((cmd&0xc000)==0) value=1000+reg;
    b[0]=pipeline[0]>>8; b[1]=pipeline[0]&255;
    pipeline[0]=pipeline[1]; pipeline[1]=value;
    return 0;
}
int main(int argc,char **argv) {
    mode=atoi(argv[1]);memcpy(regs+40,"INTAN",5);regs[62]=16;regs[63]=2;
    if(mode==3) regs[40]=0xff;
    if(mode==4) regs[63]=1;
    int rc=cb_rhd_init();
    if(mode>=1 && mode<=5) { assert(rc==-mode);assert(attempts==3);assert(calibration==0);return 0; }
    assert(rc==0 && attempts==1 && calibration==1);
    uint8_t order[4]={3,1,7,0};int16_t samples[4];
    assert(cb_rhd_read_sample(samples,order,4)==0);
    for(int i=0;i<4;i++) assert(samples[i]==1000+order[i]);
    fail_sample=1;assert(cb_rhd_read_sample(samples,order,4)==-CB_RHD_SPI);
    uint8_t frame[256];int16_t zeros[64]={0};
    size_t n=cb_encode_data(frame,0,1,zeros,4,16,0,1024,0,32768,0,0,
        CB_FLAG_SOURCE_VALID|CB_FLAG_SENSOR_FAULT|CB_FLAG_SENSOR_REASON(CB_RHD_SPI));
    assert(n<=sizeof(frame) && frame[10]==0x20 && frame[11]==0x60);
    return 0;
}
''')
    exe=root/'driver'
    subprocess.run(['cc','-std=c11','-I'+str(root),'-I'+str(ROOT/'firmware/cb_intan/src'),str(harness),str(ROOT/'firmware/cb_intan/src/cb_rhd.c'),'-o',str(exe)],check=True,capture_output=True)
    return exe

@pytest.mark.parametrize('mode',range(6))
def test_verified_driver_or_explicit_fault(driver,mode):
    subprocess.run([str(driver),str(mode)],check=True,capture_output=True)
