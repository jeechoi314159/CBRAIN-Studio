/* Included by main.c: all commands execute on the system workqueue.
 * BLE RX only adds observations under candidate_lock. */
struct candidate {
 bt_addr_le_t addr;
 uint32_t id, token, seen;
 int8_t rssi;
};
static struct candidate candidates[CB_DISCOVERY_MAX];
static struct k_spinlock candidate_lock;
static uint8_t candidate_count;
static bool candidate_overflow;
static uint32_t next_token, search_id;
static struct k_work_delayable scan_timeout;
static struct k_work_delayable connect_timeout;

static void send_event(uint8_t type, const uint8_t *payload, uint16_t len)
{
 uint8_t frame[CB_V2_MAX_PAYLOAD + 9];
 size_t n = cb_envelope(frame, type, payload, len);
 if (n) { cdc_tx_push(frame, n); }
}

/* ACK: command u8, status u8 (0 OK,1 busy,2 invalid,3 stale,
 * 4 unsupported/not negotiated,5 BLE failure,6 queue full), detail LE32. */
static void ack(uint8_t cmd, uint8_t status, uint32_t detail)
{
 uint8_t p[6] = {cmd, status}; cb_put32(p+2,detail);
 send_event(0x80,p,sizeof(p));
}
static void discovery_cancel(void)
{
 k_work_cancel_delayable(&scan_timeout);
 k_work_cancel_delayable(&connect_timeout);
 if (scanning) { scanning=false; (void)bt_le_scan_stop(); }
}
static bool parse_candidate_name(struct bt_data *data, void *user)
{
 if (data->type == BT_DATA_NAME_COMPLETE &&
     cb_parse_name(data->data,data->data_len,(uint32_t *)user)) { return false; }
 return true;
}
static void discovery_observe(const bt_addr_le_t *addr, int8_t rssi, struct net_buf_simple *ad)
{
 uint32_t id=0; bt_data_parse(ad,parse_candidate_name,&id);
 if (!id) return;
 k_spinlock_key_t key=k_spin_lock(&candidate_lock);
 if (!scanning) { k_spin_unlock(&candidate_lock,key); return; }
 uint8_t i;
 for(i=0;i<candidate_count;i++) {
  if (!bt_addr_le_cmp(addr,&candidates[i].addr)) break;
 }
 if(i==candidate_count) {
  if(i==CB_DISCOVERY_MAX) {
   candidate_overflow=true; k_spin_unlock(&candidate_lock,key); return;
  }
  candidates[i].addr=*addr;
  candidates[i].token=++next_token;
  if(!next_token) candidates[i].token=++next_token;
  candidate_count++;
 }
 if(candidates[i].id && candidates[i].id!=id) {
  candidates[i].token=++next_token;
  if(!next_token) candidates[i].token=++next_token;
 }
 candidates[i].id=id; candidates[i].rssi=rssi; candidates[i].seen=k_uptime_get_32();
 k_spin_unlock(&candidate_lock,key);
}
static void send_candidates(uint8_t reason)
{
 /* Called only after stopping the scan; copy each entry under the lock. */
 for(uint8_t i=0;i<candidate_count;i++) {
  k_spinlock_key_t key=k_spin_lock(&candidate_lock);
  struct candidate c=candidates[i];
  k_spin_unlock(&candidate_lock,key);
  uint8_t p[24];
  cb_put32(p,search_id); cb_put32(p+4,c.token); cb_put32(p+8,c.id);
  p[12]=c.addr.type; memcpy(p+13,c.addr.a.val,6); p[19]=(uint8_t)c.rssi;
  cb_put32(p+20,k_uptime_get_32()-c.seen);
  send_event(0x92,p,sizeof(p));
 }
 uint8_t done[11]; cb_put32(done,search_id);
 done[4]=reason;done[5]=candidate_count;done[6]=candidate_overflow;
 cb_put32(done+7,tx_dropped); send_event(0x93,done,sizeof(done));
}
static void scan_timeout_fn(struct k_work *work)
{
 ARG_UNUSED(work);
 scanning=false; (void)bt_le_scan_stop(); send_candidates(0);
}
static void connect_timeout_fn(struct k_work *work)
{
 ARG_UNUSED(work);
 if(control_handle) return;
 apply_target(0); address_target=false;
 if(cb_conn) {
  (void)bt_conn_disconnect(cb_conn,BT_HCI_ERR_REMOTE_USER_TERM_CONN);
 } else { (void)bt_le_scan_stop(); }
 ack(CB_CMD_CONNECT,5,ETIMEDOUT);
}
static void discovery_link_event(uint8_t state, uint8_t reason)
{
 if(!framed_mode) return;
 uint8_t p[6]={state,reason};cb_put32(p+2,target_id);
 send_event(0x94,p,sizeof(p));
 if(state==2) k_work_cancel_delayable(&connect_timeout);
 /* Retry only the selected address, bounded by a fresh timeout after a drop.
  * Failed attempts must not repeatedly extend an already running deadline. */
 if(state==0 && address_target) {
  (void)k_work_schedule(&connect_timeout,K_SECONDS(30));
 }
}
struct bridge_command { uint8_t cmd; uint8_t payload[8]; };
K_MSGQ_DEFINE(command_queue,sizeof(struct bridge_command),8,4);
static void command_work_fn(struct k_work *work)
{
 ARG_UNUSED(work);
 struct bridge_command c;
 while(k_msgq_get(&command_queue,&c,K_NO_WAIT)==0) {
  uint8_t cmd=c.cmd;
  if(!radio_ready) { if(framed_mode) ack(cmd,1,0); continue; }
  if(cmd==BR_CMD_SET_TARGET) {
   framed_mode=false; /* Existing Pairer expects raw notifications. */
   pending_target=cb_get32(c.payload);
   set_target_work_fn(NULL);
   continue;
  }
  if(cmd==CB_CMD_CAPS) {
   /* Protocol transition only while idle: no raw DATA can straddle it. */
   if(cb_conn || target_id || scanning) { if(framed_mode) ack(cmd,1,0); continue; }
   framed_mode=true;
   uint8_t caps[]={CB_DISCOVERY_VERSION,1,0,0,CB_DISCOVERY_MAX,0x0f};
   send_event(0x91,caps,sizeof(caps)); continue;
  }
  if(!framed_mode) continue; /* No unsolicited new-format bytes for old apps. */
  if(cmd==CB_CMD_SCAN) {
   if(cb_conn || target_id || scanning) { ack(cmd,1,0); continue; }
   uint16_t ms=(uint16_t)c.payload[0] | ((uint16_t)c.payload[1]<<8);
   if(ms<100 || ms>30000) { ack(cmd,2,0); continue; }
   k_work_cancel_delayable(&rescan_dwork);
   (void)bt_le_scan_stop();
   k_spinlock_key_t key=k_spin_lock(&candidate_lock);
   candidate_count=0; candidate_overflow=false;
   memset(candidates,0,sizeof(candidates));
   search_id=cb_get32(c.payload+2);scanning=true;
   k_spin_unlock(&candidate_lock,key);
   int err=bt_le_scan_start(BT_LE_SCAN_ACTIVE,device_found);
   last_scan_err=err;
   if(err) { scanning=false; ack(cmd,5,(uint32_t)err); continue; }
   k_work_reschedule(&scan_timeout,K_MSEC(ms));ack(cmd,0,search_id);
  } else if(cmd==CB_CMD_SCAN_STOP) {
   if(!scanning) { ack(cmd,2,0); continue; }
   discovery_cancel();ack(cmd,0,search_id);send_candidates(1);
  } else if(cmd==CB_CMD_LIST) {
   if(scanning || cb_conn || target_id) { ack(cmd,1,0); continue; }
   ack(cmd,0,search_id);send_candidates(2);
  } else if(cmd==CB_CMD_CONNECT) {
   if(scanning || cb_conn || target_id) { ack(cmd,1,0); continue; }
   uint32_t token=cb_get32(c.payload),id=cb_get32(c.payload+4);
   struct candidate chosen={0};bool found=false,duplicate=false;
   k_spinlock_key_t key=k_spin_lock(&candidate_lock);
   for(uint8_t i=0;i<candidate_count;i++) {
    if(candidates[i].token==token && candidates[i].id==id) { chosen=candidates[i];found=true; }
   }
   for(uint8_t i=0;found && i<candidate_count;i++) {
    if(candidates[i].id==id && bt_addr_le_cmp(&candidates[i].addr,&chosen.addr)) duplicate=true;
   }
   k_spin_unlock(&candidate_lock,key);
   if(!found || (uint32_t)(k_uptime_get_32()-chosen.seen)>10000) { ack(cmd,3,token);continue; }
   if(duplicate) { ack(cmd,2,id);continue; }
   selected_addr=chosen.addr;address_target=true;apply_target(id);
   ack(cmd,0,token);
   k_work_reschedule(&rescan_dwork,K_NO_WAIT);
   k_work_reschedule(&connect_timeout,K_SECONDS(30));
  } else if(cmd==CB_CMD_DISCONNECT) {
   discovery_cancel();k_work_cancel_delayable(&rescan_dwork);
   address_target=false;apply_target(0);
   int err=0;
   if(cb_conn) {
    err=bt_conn_disconnect(cb_conn,BT_HCI_ERR_REMOTE_USER_TERM_CONN);
   } else { (void)bt_le_scan_stop(); }
   ack(cmd,err?5:0,(uint32_t)err);
  } else { ack(cmd,4,0); }
 }
}
static K_WORK_DEFINE(command_work,command_work_fn);
static void queue_command(uint8_t cmd, const uint8_t *payload)
{
 struct bridge_command c={.cmd=cmd};
 memcpy(c.payload,payload,cb_command_size(cmd));
 if(k_msgq_put(&command_queue,&c,K_NO_WAIT)) {
  if(framed_mode) ack(cmd,6,0);
  return;
 }
 k_work_submit(&command_work);
}
static void discovery_init(void)
{
 k_work_init_delayable(&scan_timeout,scan_timeout_fn);
 k_work_init_delayable(&connect_timeout,connect_timeout_fn);
}
