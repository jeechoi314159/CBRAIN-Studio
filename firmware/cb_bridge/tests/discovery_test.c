#include <assert.h>
#include <stdio.h>
#include <errno.h>
#include "../src/discovery_protocol.h"
/* Native deterministic harness executing the actual firmware discovery module. */
typedef struct { uint8_t type; struct { uint8_t val[6]; } a; } bt_addr_le_t;
struct k_work { int unused; };
struct k_work_delayable { void (*fn)(struct k_work *); bool pending; };
struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
struct bt_data { uint8_t type; uint8_t data_len; const uint8_t *data; };
struct net_buf_simple { struct bt_data data; };
#define ARG_UNUSED(x) (void)(x)
#define BT_DATA_NAME_COMPLETE 9
#define BT_LE_SCAN_ACTIVE 1
#define BT_HCI_ERR_REMOTE_USER_TERM_CONN 0x13
#define BR_CMD_SET_TARGET 1
#define K_NO_WAIT 0
#define K_MSEC(x) (x)
#define K_SECONDS(x) ((x)*1000)
static uint32_t now_ms=1000;
static uint32_t k_uptime_get_32(void) { return now_ms; }
static int bt_addr_le_cmp(const bt_addr_le_t *a,const bt_addr_le_t *b) { return memcmp(a,b,sizeof(*a)); }
static k_spinlock_key_t k_spin_lock(struct k_spinlock *l) { (void)l; return 0; }
static void k_spin_unlock(struct k_spinlock *l,int k) { (void)l;(void)k; }
static void k_work_cancel_delayable(struct k_work_delayable *w) { w->pending=false; }
static void k_work_init_delayable(struct k_work_delayable *w,void (*fn)(struct k_work *)) { w->fn=fn; }
static void k_work_reschedule(struct k_work_delayable *w,int ms) { (void)ms;w->pending=true; }
static int k_work_schedule(struct k_work_delayable *w,int ms) { (void)ms;w->pending=true;return 1; }
static void bt_data_parse(struct net_buf_simple *ad,bool (*fn)(struct bt_data *,void *),void *user) { fn(&ad->data,user); }
static int scan_error,scan_calls,stop_calls;
static int bt_le_scan_stop(void) { stop_calls++; return 0; }
static void device_found(const bt_addr_le_t *a,int8_t r,uint8_t t,struct net_buf_simple *d) { (void)a;(void)r;(void)t;(void)d; }
static int bt_le_scan_start(int mode,void (*cb)(const bt_addr_le_t *,int8_t,uint8_t,struct net_buf_simple *)) { (void)mode;(void)cb;scan_calls++;return scan_error; }
static int bt_conn_disconnect(void *c,int reason) { (void)c;(void)reason;return 0; }
static bool framed_mode,scanning,radio_ready=true,address_target;
static uint32_t target_id,pending_target,tx_dropped;
static bt_addr_le_t selected_addr;
static void *cb_conn;
static int control_handle,last_scan_err;
static struct k_work_delayable rescan_dwork;
static void apply_target(uint32_t id) { target_id=id; }
static void set_target_work_fn(struct k_work *w) { (void)w;target_id=pending_target; }
static uint8_t output[16384];static size_t output_n;
static void cdc_tx_push(const uint8_t *p,uint16_t n) { assert(output_n+n<=sizeof(output));memcpy(output+output_n,p,n);output_n+=n; }
#define K_MSGQ_DEFINE(name,size,count,align) static struct { uint8_t data[count][size];unsigned rd,wr,n; } name
#define k_msgq_put(q,p,t) ((q)->n==8 ? -1 : (memcpy((q)->data[((q)->wr++)%8],p,sizeof(*(p))), (q)->n++,0))
#define k_msgq_get(q,p,t) (!(q)->n ? -1 : (memcpy(p,(q)->data[((q)->rd++)%8],sizeof(*(p))), (q)->n--,0))
#define K_WORK_DEFINE(name,fn) void (*name)(struct k_work *)=fn
#define k_work_submit(w) (*(w))(NULL)
#include "../src/discovery.inc"
static void command(uint8_t cmd,uint32_t a,uint32_t b) {
 uint8_t p[8];cb_put32(p,a);cb_put32(p+4,b);output_n=0;queue_command(cmd,p);
}
static void scan(uint32_t request) {
 uint8_t p[8]={0x40,0x1f};cb_put32(p+2,request);output_n=0;queue_command(CB_CMD_SCAN,p);
}
static void observe(uint8_t addr,const char *name) {
 bt_addr_le_t a={0};a.a.val[0]=addr;
 struct net_buf_simple d={.data={BT_DATA_NAME_COMPLETE,(uint8_t)strlen(name),(const uint8_t *)name}};
 discovery_observe(&a,-48,&d);
}
static void check_frames(void) {
 for(size_t off=0;off<output_n;) {
  assert(output_n-off>=9);uint8_t *p=output+off;
  assert(!memcmp(p,"\xcb\x1d\x70\x2b",4));
  size_t n=p[5]|((size_t)p[6]<<8);assert(off+n+9<=output_n);
  assert(cb_crc(p+4,n+3)==(p[n+7]|((uint16_t)p[n+8]<<8)));off+=n+9;
 }
}
int main(void) {
 uint32_t id=0;uint8_t frame[265];
 assert(cb_parse_name((const uint8_t *)"CBRAIN_4294967294",17,&id));assert(id==4294967294u);
 const char *bad[]={"CBRAIN_0","CBRAIN_01","CBRAIN_1x","CBRAIN_4294967295","CBRAIN_4294967296","CBRAIN_","xCBRAIN_1"};
 for(size_t i=0;i<sizeof(bad)/sizeof(*bad);i++) assert(!cb_parse_name((const uint8_t *)bad[i],strlen(bad[i]),&id));
 assert(cb_crc((const uint8_t *)"123456789",9)==0x29b1);
 assert(cb_envelope(frame,0x90,NULL,257)==0);
 discovery_init();command(CB_CMD_CAPS,0,0);assert(framed_mode);check_frames();
 scan(42);assert(scanning && !target_id);observe(1,"CBRAIN_1");observe(2,"CBRAIN_3");observe(1,"CBRAIN_1");
 assert(candidate_count==2 && !target_id && !cb_conn); /* no automatic connection */
 command(CB_CMD_CONNECT,1,1);assert(output[8]==1 && scanning); /* busy */
 scan_timeout_fn(NULL);assert(!scanning);check_frames();
 command(CB_CMD_CONNECT,candidates[1].token,3);assert(target_id==3 && address_target && selected_addr.a.val[0]==2);
 assert(connect_timeout.pending);discovery_link_event(2,0);assert(!connect_timeout.pending);
 discovery_link_event(0,19);assert(connect_timeout.pending);connect_timeout_fn(NULL);assert(!target_id && !address_target);
 scan(43);observe(1,"CBRAIN_1");observe(2,"CBRAIN_1");scan_timeout_fn(NULL);
 command(CB_CMD_CONNECT,candidates[0].token,1);assert(!target_id && output[8]==2); /* duplicate ID */
 scan(44);observe(1,"CBRAIN_1");scan_timeout_fn(NULL);now_ms+=10001;
 command(CB_CMD_CONNECT,candidates[0].token,1);assert(!target_id && output[8]==3);
 command(CB_CMD_CONNECT,0xabcdef,1);assert(output[8]==3);
 scan(45);for(int i=0;i<33;i++) observe(i,"CBRAIN_1");assert(candidate_count==32 && candidate_overflow);
 command(CB_CMD_SCAN_STOP,0,0);assert(!scanning);check_frames();
 command(CB_CMD_LIST,0,0);check_frames();
 scan_error=-EIO;scan(46);assert(!scanning && output[8]==5);scan_error=0;
 cb_conn=(void *)1;scan(47);assert(!scanning && output[8]==1);cb_conn=NULL;
 command(CB_CMD_DISCONNECT,0,0);assert(!target_id && !scanning);
 command(1,7,0);assert(target_id==7 && !framed_mode);
 assert(cb_command_size(1)==4 && cb_command_size(CB_CMD_SCAN)==6 && cb_command_size(CB_CMD_CONNECT)==8);
 puts("PASS: names/CRC/envelopes, idle discovery, dedup, selection, busy, duplicate IDs, stale tokens, timeout, overflow, scan failure, disconnect");
}
