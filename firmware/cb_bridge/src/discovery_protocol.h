/* Portable wire helpers, shared by firmware and native tests. */
#ifndef CB_DISCOVERY_PROTOCOL_H
#define CB_DISCOVERY_PROTOCOL_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
#define CB_DISCOVERY_VERSION 1
#define CB_DISCOVERY_MAX 32
#define CB_V2_MAX_PAYLOAD 256
#define CB_CMD_CAPS 0x10
#define CB_CMD_SCAN 0x11
#define CB_CMD_SCAN_STOP 0x12
#define CB_CMD_LIST 0x13
#define CB_CMD_CONNECT 0x14
#define CB_CMD_DISCONNECT 0x15
static inline uint32_t cb_get32(const uint8_t *p)
{ return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24); }
static inline void cb_put32(uint8_t *p, uint32_t n)
{ p[0]=n; p[1]=n>>8; p[2]=n>>16; p[3]=n>>24; }
static inline uint16_t cb_crc(const uint8_t *p, size_t n)
{
 uint16_t crc=0xffff;
 while(n--) { crc ^= (uint16_t)*p++ << 8; for(int i=0;i<8;i++) crc=(crc&0x8000)?(crc<<1)^0x1021:crc<<1; }
 return crc;
}
/* Canonical exact CBRAIN_<nonzero u32>, no prefixes, leading zeros or wildcard. */
static inline bool cb_parse_name(const uint8_t *p, size_t n, uint32_t *id)
{
 if(n<8 || n>17 || memcmp(p,"CBRAIN_",7) || p[7]=='0') return false;
 uint32_t v=0;
 for(size_t i=7;i<n;i++) {
  if(p[i]<'0'||p[i]>'9') return false;
  uint32_t digit=p[i]-'0';
  if(v>(UINT32_MAX-digit)/10) return false;
  v=v*10+digit;
 }
 if(v==UINT32_MAX) return false;
 *id=v; return true;
}
/* v2 UP: magic CB 1D 70 2B, type, payload length LE16, payload, CRC LE16.
 * CRC covers type + length + payload. Every BLE fragment gets an envelope. */
static inline size_t cb_envelope(uint8_t *out, uint8_t type, const uint8_t *p, size_t n)
{
 if(n>CB_V2_MAX_PAYLOAD) return 0;
 out[0]=0xcb;out[1]=0x1d;out[2]=0x70;out[3]=0x2b;
 out[4]=type;out[5]=n;out[6]=n>>8;
 if(n) memcpy(out+7,p,n);
 uint16_t crc=cb_crc(out+4,n+3);out[7+n]=crc;out[8+n]=crc>>8;
 return n+9;
}
static inline uint8_t cb_command_size(uint8_t cmd)
{ return cmd==1?4:cmd==CB_CMD_SCAN?6:cmd==CB_CMD_CONNECT?8:0; }
#endif
