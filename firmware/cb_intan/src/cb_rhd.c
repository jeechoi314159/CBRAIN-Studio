/*
 * RHD2216 SPI driver.  RHD2000-family 16-bit command protocol, SPI mode 0,
 * MSB-first. Each 16-bit command is one CS-framed transfer; the ADC result of
 * a CONVERT appears on MISO two commands later (2-deep pipeline).
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>
#include <zephyr/drivers/spi.h>

#include "cb_rhd.h"

/* ---- 16-bit commands (RHD2000) ---- */
#define RHD_CONVERT(ch)     (uint16_t)(((ch) & 0x3F) << 8)          /* 00 cccccc 00000000 */
#define RHD_CALIBRATE       (uint16_t)0x5500
#define RHD_CLEAR           (uint16_t)0x6A00
#define RHD_READ(reg)       (uint16_t)(0xC000 | (((reg) & 0x3F) << 8))
#define RHD_WRITE(reg, d)   (uint16_t)(0x8000 | (((reg) & 0x3F) << 8) | ((d) & 0xFF))

/* SPI mode 0 (CPOL=0,CPHA=0), MSB first, 8-bit frames (nRF SPIM); 16-bit cmd = 2 bytes. */
static const struct spi_dt_spec rhd =
	SPI_DT_SPEC_GET(DT_NODELABEL(rhd), SPI_WORD_SET(8) | SPI_TRANSFER_MSB, 0);
static int spi_error;

/*
 * Amplifier configuration (registers 0-17) — EEG/LFP band: fL = 0.1 Hz, fH = 250 Hz.
 * Bandwidth DAC values are from the RHD2000 datasheet on-chip register tables
 * (upper cutoff p.24, lower cutoff p.25). fH=250 Hz is < Nyquist (fs/2=512 Hz).
 * DSP high-pass is DISABLED so 0.1 Hz content is preserved (the amplifier already
 * has A0=0 → complete DC rejection). Output is two's-complement (matches CB enc=0).
 */
static const struct { uint8_t reg, val; } rhd_cfg[] = {
	{0,  0xDE},  /* ADC config, fast settle disabled (datasheet default) */
	{1,  0x02},  /* supply sensor off, ADC buffer bias (low rate) */
	{2,  0x04},  /* MUX bias current */
	{3,  0x00},  /* MUX load / temp sensor / aux digital out */
	{4,  0x40},  /* two's-comp output (bit6=1), DSP HPF disabled → analog fL kept */
	{5,  0x00},  /* impedance check off */
	{6,  0x00},
	{7,  0x00},
	/* upper cutoff fH = 250 Hz  (RHD2000 datasheet p.24) */
	{8,  42},    /* RH1 DAC1 */
	{9,  10},    /* RH1 DAC2 */
	{10, 5},     /* RH2 DAC1 */
	{11, 13},    /* RH2 DAC2 */
	/* lower cutoff fL = 0.10 Hz  (RHD2000 datasheet p.25): DAC1=16, DAC2=60, DAC3=1 */
	{12, 16},           /* RL DAC1 [6:0] */
	{13, (1 << 6) | 60},/* RL DAC3(bit6)=1, RL DAC2[5:0]=60  → 0x7C */
	/* per-amplifier power: RHD2216 = 16 ch (regs 14,15) */
	{14, 0xFF},  /* ch 0-7 on */
	{15, 0xFF},  /* ch 8-15 on */
	{16, 0x00},
	{17, 0x00},
};

static uint16_t rhd_xfer16(uint16_t cmd)
{
	uint8_t tx[2] = { (uint8_t)(cmd >> 8), (uint8_t)(cmd & 0xFF) };
	uint8_t rx[2] = { 0, 0 };
	const struct spi_buf txb = { .buf = tx, .len = 2 };
	const struct spi_buf rxb = { .buf = rx, .len = 2 };
	const struct spi_buf_set txs = { .buffers = &txb, .count = 1 };
	const struct spi_buf_set rxs = { .buffers = &rxb, .count = 1 };

	int err = spi_transceive_dt(&rhd, &txs, &rxs);
	if (err != 0) {
		spi_error = err;
		return 0;
	}
	return (uint16_t)((rx[0] << 8) | rx[1]);
}

static uint16_t rhd_read_reg(uint8_t reg)
{
	rhd_xfer16(RHD_READ(reg));
	rhd_xfer16(RHD_READ(63));
	return rhd_xfer16(RHD_READ(63));
}

static int rhd_init_once(void)
{
	spi_error = 0;
	if (!spi_is_ready_dt(&rhd)) {
		printk("RHD: SPI bus not ready\n");
		return -CB_RHD_BUS;
	}

	/* sync clocks, then load configuration */
	for (int i = 0; i < 4; i++) {
		rhd_xfer16(RHD_READ(63));
	}
	for (size_t i = 0; i < ARRAY_SIZE(rhd_cfg); i++) {
		rhd_xfer16(RHD_WRITE(rhd_cfg[i].reg, rhd_cfg[i].val));
	}

	/* verify comms: registers 40-44 spell "INTAN" (2-deep read pipeline) */
	uint8_t id[5];
	rhd_xfer16(RHD_READ(40));
	rhd_xfer16(RHD_READ(41));
	id[0] = rhd_xfer16(RHD_READ(42)) & 0xFF;   /* result of READ(40) */
	id[1] = rhd_xfer16(RHD_READ(43)) & 0xFF;
	id[2] = rhd_xfer16(RHD_READ(44)) & 0xFF;
	id[3] = rhd_xfer16(RHD_READ(63)) & 0xFF;   /* flush */
	id[4] = rhd_xfer16(RHD_READ(63)) & 0xFF;
	bool ok = (id[0] == 'I' && id[1] == 'N' && id[2] == 'T' &&
		   id[3] == 'A' && id[4] == 'N');
	printk("RHD: ROM=%02x %02x %02x %02x %02x SPI=%d -> %s\n", id[0], id[1], id[2], id[3], id[4], spi_error,
	       ok ? "INTAN OK" : "MISMATCH");
	if (spi_error) { return -CB_RHD_SPI; }
	if (!ok) { return -CB_RHD_ROM; }
	uint16_t model = rhd_read_reg(63), channels = rhd_read_reg(62);
	printk("RHD: model=%u channels=%u\n", model, channels);
	if (spi_error) { return -CB_RHD_SPI; }
	if (model != 2 || channels != 16) { return -CB_RHD_MODEL; }
	/* Verify the writable bits that determine format, bandwidth and channel power. */
	static const uint8_t masks[16] = {
		0xff, 0x7f, 0x3f, 0xff, 0xff, 0xff, 0xff, 0x3f,
		0xbf, 0x9f, 0xbf, 0x9f, 0xff, 0xff, 0xff, 0xff
	};
	for (size_t i = 0; i < ARRAY_SIZE(rhd_cfg); i++) {
		uint8_t reg = rhd_cfg[i].reg;
		if (reg > 15) { continue; } /* RHD2216 implements only channels 0..15. */
		uint8_t value = rhd_read_reg(reg) & 0xff;
		if (spi_error) { return -CB_RHD_SPI; }
		if ((value & masks[reg]) != (rhd_cfg[i].val & masks[reg])) {
			printk("RHD: register %u expected=%02x got=%02x\n", reg, rhd_cfg[i].val, value);
			return -CB_RHD_CONFIG;
		}
	}
	/* Datasheet: >=100 us from register setup to calibration. */
	k_busy_wait(100);

	/* ADC self-calibration: CALIBRATE then 9 dummy commands to complete */
	rhd_xfer16(RHD_CALIBRATE);
	for (int i = 0; i < 9; i++) {
		rhd_xfer16(RHD_READ(63));
	}

	return spi_error ? -CB_RHD_SPI : 0;
}

int cb_rhd_init(void)
{
	int err = -CB_RHD_BUS;
	/* Bounded boot retry covers amplifier rail settling; never invent samples. */
	for (int attempt = 0; attempt < 3; attempt++) {
		k_msleep(50);
		err = rhd_init_once();
		if (!err) { break; }
		printk("RHD: init attempt %d failed (%d)\n", attempt + 1, err);
	}
	return err;
}

int cb_rhd_read_sample(int16_t *out, const uint8_t *order, uint8_t nch)
{
	spi_error = 0;
	/* Issue CONVERT(order[0..nch-1]) plus 2 pipeline-flush commands;
	 * response[i] is the result of command[i-2] (2-deep RHD pipeline). */
	for (int i = 0; i < nch + 2; i++) {
		uint8_t ch = (i < nch) ? (order ? order[i] : (uint8_t)i) : 0;
		uint16_t resp = rhd_xfer16(RHD_CONVERT(ch));
		if (i >= 2) {
			out[i - 2] = (int16_t)resp;
		}
	}
	return spi_error ? -CB_RHD_SPI : 0;
}

int cb_rhd_read_block(int16_t *dst, const uint8_t *order, uint8_t ch_count, uint8_t spc)
{
	int16_t s[16];
	for (int k = 0; k < spc; k++) {
		int err = cb_rhd_read_sample(s, order, ch_count);
		if (err) { return err; }
		for (int c = 0; c < ch_count; c++) {
			dst[c * spc + k] = s[c];
		}
	}
	return 0;
}
