/*
 * CB_INTAN acquisition firmware (target: nRF52832)
 * ------------------------------------------------------------
 * Streams CB-protocol DATA frames over a custom GATT service, byte-compatible
 * with the CBRAIN Studio desktop decoder (cbrain_studio/core/packet.py) and
 * DongleTransport. RHD2216 failures are explicit invalid diagnostic frames;
 * experimental firmware never substitutes generated data for sensor samples.
 *
 * GATT (docs/packet_protocol.md §3.1):
 *   Service 0xCB10 · Stream 0xCB11 (notify, device->host) · Control 0xCB12 (write, host->device)
 *
 * Built with nRF Connect SDK (Zephyr). Structure follows the proven
 * cb_intan_ble_test link-test firmware.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <string.h>
#include <zephyr/sys/util.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/pwm.h>
#include <nrfx.h>

#include "cb_proto.h"
#include "cb_rhd.h"
#include "cb_config.h"
#include "cb_dsp.h"

static int rhd_error; /* zero = verified RHD2216; negative CB_RHD_* = latched fault */

/* ---- Acquisition parameters (later: driven by capability descriptor / commands) ---- */
#define CB_SR_HZ       1024      /* per-channel sampling rate */
#define CB_CH_COUNT    4         /* active channels (1..16) — RHD2216 supports up to 16 */
#define CB_SPC         16        /* samples per channel per frame (frame fits one BLE MTU) */
#define CB_TICK_HZ     32768     /* device tick rate */

/* ---- Capability descriptor (§8.4) — reported by GET_CAPS ---- */
#define CB_FW_MAJOR    0
#define CB_FW_MINOR    3                /* 0.3: verified source / explicit sensor faults */
#define CB_FW_PATCH    0
#define CB_HW_REV      1
#define CB_MAX_CH      16        /* RHD2216 physical channel count */
#define CB_LED_COUNT   2         /* board has 2 RGB LEDs */
#define CB_SYNC_INPUTS 0x01      /* bit0 = IR */
#define CB_UV_PER_LSB  0.195f           /* RHD2216 amplifier input µV/LSB */

/* ---- CB custom 128-bit UUIDs (vendor base 01CB0000-3412-109B-8A4B-E3AA8F52C3B1) ---- */
#define CB_UUID_SVC_VAL     BT_UUID_128_ENCODE(0x01cbcb10, 0x3412, 0x109b, 0x8a4b, 0xe3aa8f52c3b1)
#define CB_UUID_STREAM_VAL  BT_UUID_128_ENCODE(0x01cbcb11, 0x3412, 0x109b, 0x8a4b, 0xe3aa8f52c3b1)
#define CB_UUID_CONTROL_VAL BT_UUID_128_ENCODE(0x01cbcb12, 0x3412, 0x109b, 0x8a4b, 0xe3aa8f52c3b1)

static struct bt_uuid_128 cb_uuid_svc     = BT_UUID_INIT_128(CB_UUID_SVC_VAL);
static struct bt_uuid_128 cb_uuid_stream  = BT_UUID_INIT_128(CB_UUID_STREAM_VAL);
static struct bt_uuid_128 cb_uuid_control = BT_UUID_INIT_128(CB_UUID_CONTROL_VAL);

static struct bt_conn *current_conn;
static volatile bool   stream_enabled;   /* notifications subscribed (transport ready) */
static volatile bool   stream_on = true; /* logical streaming state (STOP/START_STREAM) */
static uint32_t        cb_seq;           /* shared frame seq for the notify link (DATA + REPLY) */

/* Active channel selection (§8.1 SET_CHMAP). `cb_active_ch` is the release point:
 * SET_CHMAP writes cb_active_ord[] first, then cb_active_ch last; the main loop
 * snapshots cb_active_ch first, then copies that many ord entries — consistent
 * per frame without a lock. */
static uint8_t          cb_active_ord[CB_MAX_CH];
static volatile uint8_t cb_active_ch = CB_CH_COUNT;

/* Runtime measurement config (from cb_config blob, else compile-time defaults). */
static uint16_t cb_sr = CB_SR_HZ;
static bool     cb_stream_ble = true;

/* Autonomous on-device LED band-power gate (from config, docs/firmware_configurator.md).
 * Every sample: shift the LED's channel into a window, FFT band-power > threshold → LED. */
struct led_gate {
	bool     enabled;
	uint8_t  src_row;        /* active-channel row feeding this LED (0xFF = channel not recorded) */
	float    band_lo, band_hi, threshold;
	uint8_t  r, g, b, intensity;
	uint16_t n, count, eval_ctr;
	int16_t  buf[CB_DSP_MAXN];
};
/* Evaluate the FFT gate every N samples (not every sample) — a per-sample FFT
 * (2× per sample) starves BLE/CPU. 32 → 32 Hz LED update (@1024 Hz), responsive. */
#define CB_GATE_EVAL_EVERY 32
static struct led_gate cb_gates[2];
static uint16_t        cb_gate_n;   /* shared FFT length for both gates */
static volatile uint16_t cb_led_flags;  /* live LED on/off in DATA-flags bits (CB_FLAG_LED0/1) */

/* Completed-block FIFO between the sampler and the notify loop (A1). Buffering a
 * few blocks absorbs transient BLE back-pressure so a slow notify RETRIES the
 * same block instead of dropping it — preserving the lossless invariant. Only a
 * sustained overload (queue full) yields a real, recorded gap. */
struct cb_block {
	uint32_t counter;
	uint16_t flags;                      /* DATA flags (LED state) captured at block time */
	uint8_t  nch;
	uint8_t  ord[CB_MAX_CH];
	int16_t  data[CB_MAX_CH * CB_SPC];   /* [ch][spc] */
};
K_MSGQ_DEFINE(block_q, sizeof(struct cb_block), 8, 4);

/* forward decls: control handler replies over the stream characteristic */
static int  cb_stream_notify(struct bt_conn *conn, const uint8_t *frame, size_t len);
static void cb_send_reply(uint8_t opcode, uint8_t status, const uint8_t *data, size_t len);
static void cb_send_caps(void);

/* ---- Device identity (provisioned per headstage) ---------------------------
 * device_id + advertised name come from UICR.CUSTOMER[0], written at flash time:
 *     nrfjprog --memwr 0x10001080 --val <n>
 * Unprovisioned (0xFFFFFFFF) -> fall back to the chip's FICR id (still unique).
 */
static uint32_t cb_device_id;
static char     cb_name[20];   /* "CBRAIN_<id>" */

static uint32_t cb_read_device_id(void)
{
	uint32_t id = NRF_UICR->CUSTOMER[0];
	if (id == 0xFFFFFFFFu || id == 0u) {
		id = NRF_FICR->DEVICEID[0];   /* fallback: unique per chip */
	}
	return id;
}

/* ---- RGB LEDs — 2× XL-3210RGBC-YG on P0.25–P0.30 (PINMAP §2), PWM 색+강도 ------
 * PWM0 ch0-3 = P0.25/26/27/28, PWM1 ch0-1 = P0.29/30 (app.overlay). 채널 듀티 =
 * 값/255. 공통애노드(active-low)라 PWM_POLARITY_INVERTED — 하드웨어에서 반대면 플립. */
#define LED_COUNT      CB_LED_COUNT     /* 2 */
#define LED_PWM_PERIOD PWM_USEC(255)    /* ~3.9 kHz, 깜빡임 없음; 듀티 0..255 */
#define LED_PWM_FLAGS  PWM_POLARITY_INVERTED

struct cb_pwm_ch { const struct device *dev; uint32_t chan; };
static const struct cb_pwm_ch cb_led_pwm[LED_COUNT][3] = {
	{ { DEVICE_DT_GET(DT_NODELABEL(pwm0)), 0 },     /* LED0 R,G,B = P0.25,26,27 */
	  { DEVICE_DT_GET(DT_NODELABEL(pwm0)), 1 },
	  { DEVICE_DT_GET(DT_NODELABEL(pwm0)), 2 } },
	{ { DEVICE_DT_GET(DT_NODELABEL(pwm0)), 3 },     /* LED1 R,G,B = P0.28,29,30 */
	  { DEVICE_DT_GET(DT_NODELABEL(pwm1)), 0 },
	  { DEVICE_DT_GET(DT_NODELABEL(pwm1)), 1 } },
};

static void cb_led_init(void)
{
	for (int l = 0; l < LED_COUNT; l++) {
		for (int c = 0; c < 3; c++) {
			if (!device_is_ready(cb_led_pwm[l][c].dev)) {
				printk("LED: pwm not ready\n");
				return;
			}
		}
	}
}

/* Per-color duty = value/255. r/g/b are FINAL 0-255 levels (강도는 호출측이 미리 반영). */
static void cb_led_set(uint8_t idx, uint8_t r, uint8_t g, uint8_t b)
{
	if (idx >= LED_COUNT) {
		return;
	}
	const uint8_t v[3] = { r, g, b };
	for (int c = 0; c < 3; c++) {
		uint32_t pulse = (uint32_t)v[c] * (uint32_t)LED_PWM_PERIOD / 255u;
		pwm_set(cb_led_pwm[idx][c].dev, cb_led_pwm[idx][c].chan,
			LED_PWM_PERIOD, pulse, LED_PWM_FLAGS);
	}
}

/* Autonomous LED gate: feed one time-sample (all active channels), evaluate each LED.
 * On → color scaled by intensity (PWM duty); off → 0. */
static void cb_gate_feed(const int16_t *s, uint8_t nch)
{
	for (int i = 0; i < 2; i++) {
		struct led_gate *g = &cb_gates[i];
		if (!g->enabled || g->src_row >= nch || g->n == 0) {
			continue;
		}
		memmove(g->buf, g->buf + 1, (g->n - 1) * sizeof(int16_t));
		g->buf[g->n - 1] = s[g->src_row];
		if (g->count < g->n) {
			g->count++;
			continue;
		}
		if (++g->eval_ctr < CB_GATE_EVAL_EVERY) {   /* decimate the FFT (64 Hz) */
			continue;
		}
		g->eval_ctr = 0;
		float p = cb_dsp_band_power(g->buf, g->n, cb_sr, g->band_lo, g->band_hi);
		if (p > g->threshold) {
			uint16_t k = g->intensity;   /* 0..100 */
			cb_led_set(i, g->r * k / 100, g->g * k / 100, g->b * k / 100);
			cb_led_flags |= (uint16_t)(CB_FLAG_LED0 << i);   /* LED0→bit8, LED1→bit9 */
		} else {
			cb_led_set(i, 0, 0, 0);
			cb_led_flags &= (uint16_t)~(CB_FLAG_LED0 << i);
		}
	}
}

/* Read the config blob at boot and apply measurement + LED gates. Falls back to
 * compile-time defaults if absent/invalid. */
static void cb_config_apply(void)
{
	const struct cb_config *c = cb_config_get();
	if (c == NULL) {
		printk("CONFIG: none — compile-time defaults\n");
		return;
	}
	if (c->sr_hz >= 128 && c->sr_hz <= 4096) {
		cb_sr = c->sr_hz;
	}
	cb_stream_ble = CB_CFG_STREAM_BLE(c);
	if (c->ch_count >= 1 && c->ch_count <= CB_MAX_CH) {
		for (uint8_t i = 0; i < c->ch_count; i++) {
			cb_active_ord[i] = c->ch_map[i];
		}
		cb_active_ch = c->ch_count;
	}
	uint16_t wsamp = (uint16_t)((uint32_t)cb_sr * c->led[0].window_ms / 1000u);
	cb_gate_n = cb_dsp_pow2_floor(wsamp ? wsamp : cb_sr / 4);
	cb_dsp_init(cb_gate_n);
	for (int i = 0; i < 2; i++) {
		struct led_gate *g = &cb_gates[i];
		g->enabled = c->led[i].enabled;
		g->band_lo = c->led[i].band_lo;
		g->band_hi = c->led[i].band_hi;
		g->threshold = c->led[i].threshold;
		g->r = c->led[i].r; g->g = c->led[i].g; g->b = c->led[i].b;
		g->intensity = c->led[i].intensity;
		g->n = cb_gate_n; g->count = 0; g->eval_ctr = 0;
		g->src_row = 0xFF;
		for (uint8_t rr = 0; rr < cb_active_ch; rr++) {
			if (cb_active_ord[rr] == c->led[i].channel) {
				g->src_row = rr;
				break;
			}
		}
	}
	printk("CONFIG: sr=%u ch=%u stream=%d gate_n=%u LED0 en=%d row=%u LED1 en=%d row=%u\n",
	       cb_sr, cb_active_ch, cb_stream_ble, cb_gate_n,
	       cb_gates[0].enabled, cb_gates[0].src_row, cb_gates[1].enabled, cb_gates[1].src_row);
}

/* ---- Control characteristic (host -> device commands, §8.1) ---- */
static ssize_t cb_control_write(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	const uint8_t *p = buf;

	if (len < 1) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}
	uint8_t opcode = p[0];
	printk("CTRL opcode=0x%02x len=%u\n", opcode, len);

	/* payload = [opcode][args...] (bare, §8.1 / P7 low-latency form). Replies go
	 * back as CB REPLY frames over the stream characteristic. Unsupported opcodes
	 * are answered with status 0xFE so the host degrades gracefully (§11). */
	switch (opcode) {
	case CB_CMD_START_STREAM:
		stream_on = true;
		cb_send_reply(opcode, CB_REPLY_OK, NULL, 0);
		break;
	case CB_CMD_STOP_STREAM:
		stream_on = false;
		cb_send_reply(opcode, CB_REPLY_OK, NULL, 0);
		break;
	case CB_CMD_GET_CAPS:
		cb_send_caps();
		break;
	case CB_CMD_GET_FW_VER: {
		uint8_t v[4] = { CB_FW_MAJOR, CB_FW_MINOR, CB_FW_PATCH, CB_VER };
		cb_send_reply(opcode, CB_REPLY_OK, v, sizeof(v));
		break;
	}
	case CB_CMD_SET_CHMAP: {
		/* args = [ch_map u16][ord_len u8][ord…]; ord[i] = physical channel index. */
		if (len < 4) {
			cb_send_reply(opcode, CB_REPLY_UNSUPPORTED, NULL, 0);
			break;
		}
		uint8_t ord_len = p[3];
		bool ok = (ord_len >= 1 && ord_len <= CB_MAX_CH && len >= 4u + ord_len);
		for (uint8_t i = 0; ok && i < ord_len; i++) {
			if (p[4 + i] >= CB_MAX_CH) {
				ok = false;
			}
		}
		if (!ok) {
			cb_send_reply(opcode, CB_REPLY_UNSUPPORTED, NULL, 0);
			break;
		}
		for (uint8_t i = 0; i < ord_len; i++) {
			cb_active_ord[i] = p[4 + i];
		}
		cb_active_ch = ord_len;      /* release: main loop picks this up next frame */
		printk("SET_CHMAP: %u channels\n", ord_len);
		cb_send_reply(opcode, CB_REPLY_OK, NULL, 0);
		break;
	}
	case CB_CMD_SET_LED: {
		/* args = [led_index][r][g][b]; index validated against led_count (§8.1). */
		if (len < 5 || p[1] >= LED_COUNT) {
			cb_send_reply(opcode, CB_REPLY_UNSUPPORTED, NULL, 0);
			break;
		}
		cb_led_set(p[1], p[2], p[3], p[4]);
		cb_send_reply(opcode, CB_REPLY_OK, NULL, 0);
		break;
	}
	default:
		/* SET_SR_HZ / GET_BAT: not yet implemented (A3, follow-ups). */
		cb_send_reply(opcode, CB_REPLY_UNSUPPORTED, NULL, 0);
		break;
	}
	return len;
}

static void stream_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	stream_enabled = (value == BT_GATT_CCC_NOTIFY);
	printk("Stream notifications %s\n", stream_enabled ? "ENABLED" : "disabled");
}

/* Service layout: attrs[0]=svc, [1]=stream chrc, [2]=stream value, [3]=CCC,
 *                 [4]=control chrc, [5]=control value. Notify on attrs[2]. */
BT_GATT_SERVICE_DEFINE(cb_svc,
	BT_GATT_PRIMARY_SERVICE(&cb_uuid_svc),
	BT_GATT_CHARACTERISTIC(&cb_uuid_stream.uuid, BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE, NULL, NULL, NULL),
	BT_GATT_CCC(stream_ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(&cb_uuid_control.uuid,
			       BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_WRITE, NULL, cb_control_write, NULL),
);

#define CB_STREAM_ATTR (&cb_svc.attrs[2])

/* ---- Advertising (name filled at runtime from cb_name) ---- */
static const uint8_t ad_flags = BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR;
static struct bt_data ad[2];
static const struct bt_data sd[] = {
	BT_DATA_BYTES(BT_DATA_UUID128_ALL, CB_UUID_SVC_VAL),
};

/* 광고 재개는 워크큐에서 + 실패 시 재시도.
 * disconnected() 콜백에서 직접 bt_le_adv_start() 하면 연결 리소스가 아직 해제되지 않아
 * 실패할 수 있고, 재시도가 없으면 **재부팅 전까지 영영 광고를 안 한다**(호스트가 재연결 불가). */
static struct k_work_delayable adv_dwork;

/* 광고 워치독: 연결돼 있지 않으면 항상 광고가 켜져 있도록 주기적으로 보장한다.
 * (어떤 이유로 광고가 멈추든 — start 실패, 내부 상태 등 — 2초 안에 복구되므로
 *  호스트가 항상 재연결할 수 있다. 이미 광고 중이면 -EALREADY 로 무해.) */
static void adv_work_fn(struct k_work *w)
{
	ARG_UNUSED(w);
	if (!current_conn) {
		int err = bt_le_adv_start(BT_LE_ADV_CONN_FAST_2, ad, ARRAY_SIZE(ad),
					  sd, ARRAY_SIZE(sd));
		if (err && err != -EALREADY) {
			printk("adv start failed (err %d)\n", err);
		}
	}
	(void)k_work_reschedule(&adv_dwork, K_SECONDS(2));   /* 계속 감시 */
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err) {
		printk("Connection failed (err %u)\n", err);
		return;
	}
	printk("Connected\n");
	current_conn = bt_conn_ref(conn);
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	printk("Disconnected (reason %u)\n", reason);
	stream_enabled = false;
	k_msgq_purge(&block_q);                 /* drop queued blocks — fresh stream on reconnect */
	for (int l = 0; l < LED_COUNT; l++) {   /* no stuck LED after the host leaves */
		cb_led_set(l, 0, 0, 0);
	}
	if (current_conn) {
		bt_conn_unref(current_conn);
		current_conn = NULL;
	}
	/* Connectable advertising stops on connect; resume it so the host can reconnect
	 * without a power cycle. 콜백에서 직접 start 하지 말고 워크큐+재시도로(위 adv_work_fn). */
	(void)k_work_reschedule(&adv_dwork, K_NO_WAIT);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected    = connected,
	.disconnected = disconnected,
};

/* Notify a whole CB frame, split into <=(MTU-3) chunks. The desktop Decoder
 * reassembles by CB magic + length + CRC, so multi-notification frames are fine. */
static int cb_stream_notify(struct bt_conn *conn, const uint8_t *frame, size_t len)
{
	uint16_t mtu = bt_gatt_get_mtu(conn);
	size_t chunk = (mtu > 3) ? (size_t)(mtu - 3) : 20;

	for (size_t off = 0; off < len; off += chunk) {
		size_t n = MIN(chunk, len - off);
		int err = bt_gatt_notify(conn, CB_STREAM_ATTR, frame + off, n);
		if (err) {
			return err;
		}
	}
	return 0;
}

/* Send a REPLY frame over the stream characteristic (device→host). Uses the
 * shared link seq so the host sees one continuous sequence across DATA + REPLY.
 * Called from the control-write (BT RX) context; safe to notify from there. */
static void cb_send_reply(uint8_t opcode, uint8_t status, const uint8_t *data, size_t len)
{
	static uint8_t reply_frame[128];
	if (!(current_conn && stream_enabled)) {
		return;   /* host hasn't subscribed — nothing to notify */
	}
	size_t flen = cb_encode_reply(reply_frame, cb_seq, opcode, status, data, len);
	if (cb_stream_notify(current_conn, reply_frame, flen) == 0) {
		cb_seq++;
	}
}

static void cb_send_caps(void)
{
	static uint8_t caps_frame[128];
	static const uint16_t rates[] = { CB_SR_HZ };
	if (!(current_conn && stream_enabled)) {
		return;
	}
	size_t flen = cb_encode_caps(caps_frame, cb_seq,
				     CB_FW_MAJOR, CB_FW_MINOR, CB_FW_PATCH, CB_HW_REV,
				     CB_MAX_CH, CB_LED_COUNT, CB_SYNC_INPUTS, CB_ENC_I16,
				     CB_UV_PER_LSB, rates, (uint8_t)ARRAY_SIZE(rates));
	if (cb_stream_notify(current_conn, caps_frame, flen) == 0) {
		cb_seq++;
	}
}

/* ---- Sample-accurate acquisition (A1) ---------------------------------------
 * A periodic k_timer at fs gives `sample_sem` once per sample. A high-priority
 * sampler thread reads exactly ONE time-sample per tick (uniform spacing, no
 * intra-frame bunching, no long-run drift). Completed blocks go into `block_q`;
 * main() drains the queue and notifies (retrying on BLE back-pressure so blocks
 * are never dropped). Absolute fs is bounded by the RC LFCLK (±500 ppm). */
static K_SEM_DEFINE(sample_sem, 0, 4);
static void sample_tick(struct k_timer *t) { k_sem_give(&sample_sem); }
static K_TIMER_DEFINE(sample_timer, sample_tick, NULL);

static struct cb_block cur;                      /* filled by the sampler */
static uint32_t        cb_dropped;               /* blocks lost to queue-full overload */

static void sampler_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
	uint8_t nch = CB_CH_COUNT, ord[CB_MAX_CH];
	uint32_t counter = 0;
	int16_t s[CB_MAX_CH];
	int k = 0;

	for (;;) {
		k_sem_take(&sample_sem, K_FOREVER);          /* wait for the next fs tick */
		if (k == 0) {                                /* snapshot channels per frame */
			nch = cb_active_ch;
			for (uint8_t i = 0; i < nch; i++) {
				ord[i] = cb_active_ord[i];
			}
		}
		if (!rhd_error) {
			rhd_error = cb_rhd_read_sample(s, ord, nch);
			if (rhd_error) { printk("RHD: acquisition stopped (%d)\n", rhd_error); }
		}
		if (rhd_error) {
			/* Invalid placeholders carry only fault/identity information. Never
			 * feed them into an LED gate or present them as measured zeros. */
			memset(s, 0, sizeof(s));
			for (uint8_t l = 0; l < CB_LED_COUNT; l++) { cb_led_set(l, 0, 0, 0); }
			cb_led_flags = 0;
		} else {
			cb_gate_feed(s, nch);
		}
		for (uint8_t cc = 0; cc < nch; cc++) {
			cur.data[cc * CB_SPC + k] = s[cc];    /* scatter into [ch][spc] */
		}
		if (++k >= CB_SPC) {
			cur.counter = counter;
			cur.flags = (uint16_t)(CB_FLAG_LED_VALID | CB_FLAG_SOURCE_VALID | cb_led_flags);
			if (rhd_error) {
				cur.flags |= CB_FLAG_SENSOR_FAULT | CB_FLAG_SENSOR_REASON(-rhd_error);
				memset(cur.data, 0, sizeof(cur.data)); /* invalidate entire block */
			}
			cur.nch = nch;
			for (uint8_t i = 0; i < nch; i++) {
				cur.ord[i] = ord[i];
			}
			counter += CB_SPC;
			k = 0;
			/* stream DATA only if config-enabled AND a host is subscribed */
			if (cb_stream_ble && stream_enabled && current_conn && stream_on) {
				if (k_msgq_put(&block_q, &cur, K_NO_WAIT) != 0) {
					cb_dropped++;
				}
			}
		}
	}
}

#define SAMPLER_STACK 2048   /* CMSIS-DSP FFT 여유 */
static K_THREAD_STACK_DEFINE(sampler_stack, SAMPLER_STACK);
static struct k_thread sampler_tcb;

int main(void)
{
	static uint8_t  frame[CB_HDR_SIZE + CB_DATA_HDR + CB_MAX_CH * CB_SPC * 2 + 3 + CB_CRC_SIZE];
	int err;

	for (uint8_t i = 0; i < CB_CH_COUNT; i++) {   /* default channel order 0..N-1 */
		cb_active_ord[i] = i;
	}
	cb_led_init();

	cb_device_id = cb_read_device_id();
	int nlen = snprintk(cb_name, sizeof(cb_name), "CBRAIN_%u", (unsigned)cb_device_id);
	printk("CB_INTAN firmware — id=%u name=\"%s\"\n", (unsigned)cb_device_id, cb_name);

	err = bt_enable(NULL);
	if (err) {
		printk("bt_enable failed (err %d)\n", err);
		return 0;
	}
	(void)bt_set_name(cb_name);   /* needs CONFIG_BT_DEVICE_NAME_DYNAMIC */

	ad[0] = (struct bt_data)BT_DATA(BT_DATA_FLAGS, &ad_flags, 1);
	ad[1] = (struct bt_data)BT_DATA(BT_DATA_NAME_COMPLETE, cb_name, nlen);

	k_work_init_delayable(&adv_dwork, adv_work_fn);
	(void)k_work_reschedule(&adv_dwork, K_SECONDS(2));   /* 광고 워치독 상시 가동 */

	err = bt_le_adv_start(BT_LE_ADV_CONN_FAST_2, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
	if (err) {
		printk("Advertising failed to start (err %d)\n", err);
		return 0;
	}
	printk("Advertising as \"%s\" (CB service 0xCB10)\n", cb_name);

	rhd_error = cb_rhd_init();
	printk("Sample source: %s (error=%d)\n", rhd_error ? "INVALID: RHD failure" : "RHD2216 verified", rhd_error);

	cb_config_apply();   /* read config blob → measurement + autonomous LED gates */

	/* Start the fs timer (cb_sr from config) and the sampler. The gate runs from
	 * boot, so LEDs are autonomous even with no BLE host connected. */
	k_thread_create(&sampler_tcb, sampler_stack, SAMPLER_STACK, sampler_thread,
			NULL, NULL, NULL, K_PRIO_COOP(7), 0, K_NO_WAIT);
	k_timer_start(&sample_timer, K_USEC(1000000 / cb_sr), K_USEC(1000000 / cb_sr));

	/* Drain the block FIFO and notify. On BLE back-pressure, retry the SAME block
	 * (it stays in `b`) instead of dropping it — the sampler keeps buffering into
	 * block_q meanwhile. */
	static struct cb_block b;
	while (1) {
		k_msgq_get(&block_q, &b, K_FOREVER);
		size_t flen = cb_encode_data(frame, cb_seq, cb_device_id, b.data,
					     b.nch, CB_SPC, b.counter, cb_sr,
					     b.counter, CB_TICK_HZ, CB_ENC_I16, b.ord, b.flags);
		while (cb_stream_notify(current_conn, frame, flen) != 0) {
			if (!(current_conn && stream_enabled)) {
				break;   /* disconnected — abandon (queue purged on disconnect) */
			}
			k_msleep(2);     /* BLE TX busy; retry the same block */
		}
		cb_seq++;
	}
	return 0;
}
