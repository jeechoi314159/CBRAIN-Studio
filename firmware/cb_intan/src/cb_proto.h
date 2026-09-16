/*
 * CB packet protocol — device-side encoder (nRF52832).
 * Byte-compatible with the desktop decoder (cbrain_studio/core/packet.py,
 * docs/packet_protocol.md). All integers little-endian.
 *
 * Frame = [ cb_hdr 10B ][ payload ][ CRC16 2B ],  CRC16-CCITT(F) over [hdr..payload].
 * DATA payload = data_hdr(44B) + events(0) + samples(ch*spc*2) + pad(0..3).
 */
#ifndef CB_PROTO_H
#define CB_PROTO_H

#include <stdint.h>
#include <stddef.h>

#define CB_MAGIC0   'C'
#define CB_MAGIC1   'B'
#define CB_VER      2          /* wire protocol version field (compat with desktop) */
#define CB_HDR_SIZE 10
#define CB_DATA_HDR 44
#define CB_ORD_LEN  16
#define CB_CRC_SIZE 2

/* frame types (§4.3) */
#define CB_T_DATA    0x01
#define CB_T_SYNC    0x02
#define CB_T_STATUS  0x03
#define CB_T_REPLY   0x04
#define CB_T_COMMAND 0x10

/* sample encoding (§6.4): 0 = i16 two's-complement, 1 = offset-binary legacy */
#define CB_ENC_I16   0

/* DATA header `flags` bits (§6.1). bit0/1 = OVFL/DROPPED (host-status, reserved).
 * bit8/9 carry the device's real LED0/LED1 on/off; bit15 marks that this device
 * reports LED state at all — older firmware leaves flags = 0, so the host must
 * check CB_FLAG_LED_VALID before trusting the LED bits. */
#define CB_FLAG_OVFL       (1u << 0)
#define CB_FLAG_DROPPED    (1u << 1)
#define CB_FLAG_LED0       (1u << 8)
#define CB_FLAG_LED1       (1u << 9)
#define CB_FLAG_LED_VALID  (1u << 15)
/* Since 0.3.0: valid source report; FAULT means samples are INVALID placeholders.
 * Fault reason bits 4..6 match CB_RHD_* in cb_rhd.h. No synthetic fallback. */
#define CB_FLAG_SOURCE_VALID (1u << 14)
#define CB_FLAG_SENSOR_FAULT (1u << 13)
#define CB_FLAG_SENSOR_REASON(reason) (((uint16_t)(reason) & 7u) << 4)

/* command opcodes (§8.1) — host→device, bare [opcode][args] on the control char */
#define CB_CMD_SET_CHMAP     0x20
#define CB_CMD_SET_SR_HZ     0x21
#define CB_CMD_START_STREAM  0x24
#define CB_CMD_STOP_STREAM   0x25
#define CB_CMD_GET_CAPS      0x31
#define CB_CMD_GET_BAT       0x41
#define CB_CMD_GET_FW_VER    0x42
#define CB_CMD_SET_LED       0x50

/* REPLY status (§8.2): 0x00 OK, 0xFE unknown/unsupported opcode */
#define CB_REPLY_MARKER      0xB0
#define CB_REPLY_OK          0x00
#define CB_REPLY_UNSUPPORTED 0xFE

/* CRC16-CCITT(F): poly 0x1021, init 0xFFFF, no reflection, xorout 0. */
static inline uint16_t cb_crc16(const uint8_t *d, size_t n)
{
	uint16_t crc = 0xFFFF;
	for (size_t i = 0; i < n; i++) {
		crc ^= (uint16_t)d[i] << 8;
		for (int b = 0; b < 8; b++) {
			crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
					     : (uint16_t)(crc << 1);
		}
	}
	return crc;
}

static inline void cb_put_u16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
static inline void cb_put_u32(uint8_t *p, uint32_t v)
{
	p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24;
}

/*
 * Encode one DATA frame into `out` (needs >= CB_HDR_SIZE+CB_DATA_HDR+ch*spc*2+3+2).
 * `ch_samples` is row-major [ch][spc] (channel-major); on the wire it is written
 * spc-major/channel-minor to match the desktop `channels.T` layout.
 * `order` (len >= ch_count) lists the physical channel index of each row; NULL
 * means identity 0..ch_count-1. ch_map / ord[] on the wire follow `order`.
 * Returns the total frame length in bytes.
 */
static inline size_t cb_encode_data(uint8_t *out, uint32_t seq, uint32_t device_id,
				    const int16_t *ch_samples, uint8_t ch_count, uint8_t spc,
				    uint32_t first_counter, uint16_t sr_hz,
				    uint32_t t0_tick, uint32_t tick_hz, uint8_t enc,
				    const uint8_t *order, uint16_t flags)
{
	uint16_t ch_map = 0;
	for (int c = 0; c < ch_count; c++) {
		uint8_t phys = order ? order[c] : (uint8_t)c;
		ch_map |= (uint16_t)(1u << (phys & 0x0F));
	}
	size_t data_len = (size_t)ch_count * spc * 2;
	size_t pre = CB_HDR_SIZE + CB_DATA_HDR + data_len;   /* events_len = 0 */
	uint8_t pad = (uint8_t)((4 - ((pre + CB_CRC_SIZE) & 3)) & 3);
	uint16_t total = (uint16_t)(pre + pad + CB_CRC_SIZE);

	uint8_t *p = out;
	/* --- frame header (10B) --- */
	*p++ = CB_MAGIC0; *p++ = CB_MAGIC1; *p++ = CB_VER; *p++ = CB_T_DATA;
	cb_put_u32(p, seq);   p += 4;
	cb_put_u16(p, total); p += 2;
	/* --- data_hdr fixed 28B (§6.1) --- */
	cb_put_u16(p, flags);        p += 2;   /* flags (§6.1: LED state in bit8/9/15) */
	cb_put_u32(p, device_id);    p += 4;
	cb_put_u16(p, sr_hz);        p += 2;
	cb_put_u16(p, ch_map);       p += 2;
	*p++ = ch_count;
	*p++ = spc;
	cb_put_u32(p, first_counter); p += 4;
	cb_put_u32(p, t0_tick);      p += 4;
	cb_put_u32(p, tick_hz);      p += 4;
	*p++ = enc;
	*p++ = 0;                    /* events_len */
	*p++ = ch_count;             /* ord_len */
	*p++ = pad;
	/* --- ord[16] --- */
	for (int i = 0; i < CB_ORD_LEN; i++) {
		*p++ = (i < ch_count) ? (order ? order[i] : (uint8_t)i) : 0xFF;
	}
	/* --- samples: spc-major, channel-minor --- */
	for (int k = 0; k < spc; k++) {
		for (int c = 0; c < ch_count; c++) {
			cb_put_u16(p, (uint16_t)ch_samples[c * spc + k]);
			p += 2;
		}
	}
	/* --- pad --- */
	for (int i = 0; i < pad; i++) {
		*p++ = 0;
	}
	/* --- CRC16 over [out .. p) --- */
	cb_put_u16(p, cb_crc16(out, (size_t)(p - out)));
	p += CB_CRC_SIZE;
	return (size_t)(p - out);
}

/*
 * Encode a REPLY frame (type 0x04, §8.2): payload = [0xB0][opcode][status][data…].
 * Sent device→host over the Stream characteristic; the desktop Decoder dispatches
 * by type and parse_capabilities() reads GET_CAPS reply_data. Returns frame length.
 * `out` needs >= CB_HDR_SIZE + 3 + data_len + CB_CRC_SIZE.
 */
static inline size_t cb_encode_reply(uint8_t *out, uint32_t seq, uint8_t opcode,
				     uint8_t status, const uint8_t *data, size_t data_len)
{
	uint16_t total = (uint16_t)(CB_HDR_SIZE + 3 + data_len + CB_CRC_SIZE);
	uint8_t *p = out;
	*p++ = CB_MAGIC0; *p++ = CB_MAGIC1; *p++ = CB_VER; *p++ = CB_T_REPLY;
	cb_put_u32(p, seq);   p += 4;
	cb_put_u16(p, total); p += 2;
	*p++ = CB_REPLY_MARKER;
	*p++ = opcode;
	*p++ = status;
	for (size_t i = 0; i < data_len; i++) {
		*p++ = data[i];
	}
	cb_put_u16(p, cb_crc16(out, (size_t)(p - out)));
	p += CB_CRC_SIZE;
	return (size_t)(p - out);
}

/*
 * Encode a GET_CAPS REPLY (§8.4). The capability descriptor is the single source
 * of truth that makes the desktop hardware-agnostic (channel/LED/rate counts).
 */
static inline size_t cb_encode_caps(uint8_t *out, uint32_t seq,
				    uint8_t fw_major, uint8_t fw_minor, uint8_t fw_patch,
				    uint8_t hw_rev, uint8_t max_channels, uint8_t led_count,
				    uint8_t sync_inputs, uint8_t enc, float uv_per_lsb,
				    const uint16_t *rates, uint8_t n_rates)
{
	uint8_t d[14 + 2 * 16];   /* descriptor: 14B fixed + rates (<=16) */
	uint8_t *q = d;
	*q++ = CB_VER;                 /* proto_ver */
	*q++ = fw_major; *q++ = fw_minor; *q++ = fw_patch;
	*q++ = hw_rev;
	*q++ = max_channels;
	*q++ = led_count;
	*q++ = sync_inputs;
	*q++ = enc;
	uint32_t uv_bits;              /* IEEE-754 f32 little-endian */
	__builtin_memcpy(&uv_bits, &uv_per_lsb, sizeof(uv_bits));
	cb_put_u32(q, uv_bits); q += 4;
	*q++ = n_rates;
	for (uint8_t i = 0; i < n_rates; i++) {
		cb_put_u16(q, rates[i]); q += 2;
	}
	return cb_encode_reply(out, seq, CB_CMD_GET_CAPS, CB_REPLY_OK, d, (size_t)(q - d));
}

#endif /* CB_PROTO_H */
