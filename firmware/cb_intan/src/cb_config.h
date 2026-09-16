/*
 * CB 펌웨어 설정 blob — GUI 가 찍고(cbrain_studio/app/fw_config.py) 펌웨어가 읽는 계약.
 * 플래시 예약 페이지(CB_CONFIG_ADDR)에 memory-mapped 로 존재. 부팅 시 읽어 검증.
 * 레이아웃/CRC 는 fw_config.py 와 바이트 호환(little-endian, packed). docs/firmware_configurator.md.
 */
#ifndef CB_CONFIG_H
#define CB_CONFIG_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include "cb_proto.h"   /* cb_crc16 */

#define CB_CONFIG_ADDR      0x70000u        /* 앱 위 · storage 파티션(0x7a000+) 아래 자유영역 */
#define CB_CONFIG_VERSION   1

/* LED 게이트는 샘플 단위: 매 샘플 직전 window_ms 구간 대역파워 > threshold → ON. */
struct __attribute__((packed)) cb_led_config {
	uint8_t  enabled;
	uint8_t  channel;
	uint16_t window_ms;
	float    band_lo;
	float    band_hi;
	float    threshold;      /* GUI 측정 절대 대역파워 임계 */
	uint8_t  r, g, b;
	uint8_t  intensity;      /* 0–100 % */
};

struct __attribute__((packed)) cb_config {
	/* header (8) — crc 는 body(header 이후 전체) 대상 */
	uint8_t  magic[4];       /* "CBCF" */
	uint16_t version;
	uint16_t crc;
	/* measurement (24) */
	uint16_t sr_hz;
	uint8_t  notch;          /* 0 off / 50 / 60 */
	uint8_t  bw_preset;      /* 0 = 0.1–250 Hz */
	uint8_t  ch_count;
	uint8_t  flags;          /* bit0 = stream_ble */
	uint16_t _pad;
	uint8_t  ch_map[16];     /* 0xFF = 미사용 */
	/* led[2] (28 each) */
	struct cb_led_config led[2];
};

_Static_assert(sizeof(struct cb_led_config) == 20, "cb_led_config layout");
_Static_assert(sizeof(struct cb_config) == 72, "cb_config layout (must match fw_config.py)");

#define CB_CFG_STREAM_BLE(c)  (((c)->flags & 0x01u) != 0)

/*
 * 플래시에서 설정을 읽어 검증. 유효하면 포인터, 아니면 NULL(→ 펌웨어 기본값 폴백).
 * blob 이 안 찍혀 있으면 지워진 플래시(0xFF) → magic 불일치 → NULL.
 */
static inline const struct cb_config *cb_config_get(void)
{
	const struct cb_config *c = (const struct cb_config *)CB_CONFIG_ADDR;
	if (memcmp(c->magic, "CBCF", 4) != 0) {
		return NULL;
	}
	if (c->version != CB_CONFIG_VERSION) {
		return NULL;
	}
	const uint8_t *body = (const uint8_t *)&c->sr_hz;      /* header 8B 이후 */
	size_t body_len = sizeof(struct cb_config) - 8;
	if (cb_crc16(body, body_len) != c->crc) {
		return NULL;
	}
	return c;
}

#endif /* CB_CONFIG_H */
