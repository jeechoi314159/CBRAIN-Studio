/*
 * CB Bridge — nRF52840 동글: 단일링크 BLE central ↔ USB CDC ACM 투명 브리지.
 *
 * 배포 모델: 1 동글 = 1 헤드스테이지(배타적). 한 PC 에 USB 허브로 동글 8개 → 8마리 동시 기록.
 *
 * 동작:
 *   · 배포용 이미지는 대기 상태로 부팅. 기존 SET_TARGET 또는 신규 검색/후보 선택 명령 사용.
 *   · Mac BLE 없이 동글만 검색. 신규 앱은 CRC/길이 프레임으로 제어와 DATA를 분리.
 *   · 그 이름을 스캔→연결→CB 서비스(0xCB10) 발견→Stream(0xCB11) 구독, Control(0xCB12) 핸들 확보.
 *   · UP:   0xCB11 notify 바이트 → USB-CDC 로 그대로 (호스트 Decoder 가 재조립/ device_id demux).
 *   · DOWN: USB-CDC 수신 바이트 → 0xCB12 write (호스트가 보내는 bare 커맨드 [opcode][args]).
 *   · 끊기면 자동 재스캔/재연결.
 *
 * 링크가 1개뿐이라 CDC 스트림에 프레임이 섞일 일이 없다 → notify 바이트를 그대로 흘리면 된다.
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/usb/usb_device.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>
#include <zephyr/bluetooth/hci.h>
#include <nrfx.h>
#include <stdio.h>
#include <string.h>
#include "discovery_protocol.h"

LOG_MODULE_REGISTER(cb_bridge, LOG_LEVEL_INF);

/* ---- CB GATT (헤드스테이지와 동일 base 01cbXXXX-3412-109b-8a4b-e3aa8f52c3b1) ---- */
#define CB_UUID_STREAM_VAL  BT_UUID_128_ENCODE(0x01cbcb11, 0x3412, 0x109b, 0x8a4b, 0xe3aa8f52c3b1)
#define CB_UUID_CONTROL_VAL BT_UUID_128_ENCODE(0x01cbcb12, 0x3412, 0x109b, 0x8a4b, 0xe3aa8f52c3b1)
static struct bt_uuid_128 cb_uuid_stream  = BT_UUID_INIT_128(CB_UUID_STREAM_VAL);
static struct bt_uuid_128 cb_uuid_control = BT_UUID_INIT_128(CB_UUID_CONTROL_VAL);

/* ---- USB CDC ACM ---- */
static const struct device *cdc_dev;
RING_BUF_DECLARE(tx_ring, 16384);  /* BLE notify → CDC (up) */
RING_BUF_DECLARE(rx_ring, 512);    /* CDC → BLE (down) */
static K_SEM_DEFINE(rx_sem, 0, 1);
static uint32_t tx_dropped;
static struct k_spinlock tx_lock;
static bool framed_mode;
static bool scanning;
static bool radio_ready;
static bool address_target;
static bt_addr_le_t selected_addr;
static void discovery_cancel(void);
static void discovery_observe(const bt_addr_le_t *addr, int8_t rssi, struct net_buf_simple *ad);
static void queue_command(uint8_t cmd, const uint8_t *payload);
static void send_event(uint8_t type, const uint8_t *payload, uint16_t len);
static void discovery_link_event(uint8_t state, uint8_t reason);

static void cdc_isr(const struct device *dev, void *user)
{
	ARG_UNUSED(user);
	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
		if (uart_irq_rx_ready(dev)) {
			uint8_t buf[64];
			int n;
			while ((n = uart_fifo_read(dev, buf, sizeof(buf))) > 0) {
				ring_buf_put(&rx_ring, buf, n);
			}
			k_sem_give(&rx_sem);
		}
		if (uart_irq_tx_ready(dev)) {
			/* FIFO 가 받은 만큼만 링에서 소비 — fifo_fill 은 요청보다 적게 채울 수
			 * 있으므로 claim→fill→finish(sent) 로 바이트 유실을 막는다(핵심). */
			k_spinlock_key_t key = k_spin_lock(&tx_lock);
			uint8_t *data;
			uint32_t claimed = ring_buf_get_claim(&tx_ring, &data, 64);
			if (claimed == 0) {
				uart_irq_tx_disable(dev);
			} else {
				int sent = uart_fifo_fill(dev, data, claimed);
				ring_buf_get_finish(&tx_ring, sent < 0 ? 0 : sent);
			}
			k_spin_unlock(&tx_lock, key);
		}
	}
}

static void cdc_tx_push(const uint8_t *data, uint16_t len)
{
 k_spinlock_key_t key = k_spin_lock(&tx_lock);
 /* Never enqueue a partial envelope; keep multiple producers serialized. */
 if (ring_buf_space_get(&tx_ring) >= len) {
  ring_buf_put(&tx_ring, data, len);
 } else {
  tx_dropped += len;
 }
 uart_irq_tx_enable(cdc_dev);
 k_spin_unlock(&tx_lock, key);
}

/* ---- BLE central ---- */
static struct bt_conn *cb_conn;
static uint16_t control_handle;
static struct bt_gatt_subscribe_params sub_params;
static struct bt_gatt_discover_params disc_params;
static struct bt_uuid_128 disc_uuid;         /* discovery 진행용 스크래치 */
static enum { D_STREAM_CHR, D_STREAM_CCC, D_CONTROL_CHR } disc_step;

static uint32_t target_id;
static char target_name[20];

/* ---- 타깃 각인 blob (naive 동글: 컴파일 없이 hex 스탬프로 동글별 타깃 지정) ----
 * tools/stamp_bridge_target.py 가 magic "CBTGTv1\0" 뒤 4바이트(target id, LE)를 덮어쓴다.
 * → base hex 한 번 빌드 → 동글마다 스탬프만. volatile const = 상수전파 방지(실 flash 읽기),
 * used = GC 방지. target=0 이면 UICR 폴백, 그것도 없으면 "아무 CBRAIN"(브링업). */
__attribute__((used))
static volatile const struct {
	char     magic[8];
	uint32_t target;
} cb_target_blob = { { 'C', 'B', 'T', 'G', 'T', 'v', '1', '\0' }, 0u };

/* ---- 런타임 페어링 값 (호스트가 CDC SET_TARGET 으로 설정 → flash 영속) ---- */
static uint32_t stored_target;

static int cb_settings_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	ARG_UNUSED(len);
	if (settings_name_steq(name, "target", NULL)) {
		(void)read_cb(cb_arg, &stored_target, sizeof(stored_target));
		return 0;
	}
	return -ENOENT;
}
SETTINGS_STATIC_HANDLER_DEFINE(cb_bridge_settings, "cb", NULL, cb_settings_set, NULL, NULL);

/* 0 = 미페어링(대기, 스캔 안 함). 0xFFFFFFFF = 와일드카드(아무 CBRAIN_* 에 자동 연결).
 * 그 외 = 특정 번호. */
#define CB_TARGET_ANY 0xFFFFFFFFu

static void apply_target(uint32_t id)
{
	if (id == CB_TARGET_ANY) {
		target_id = CB_TARGET_ANY;             /* nonzero → 스캔함; 이름은 접두 매칭 */
		strcpy(target_name, "CBRAIN_*");
	} else if (id == 0u) {
		target_id = 0;
		strcpy(target_name, "CBRAIN");                 /* 미페어링: 대기(스캔 안 함) */
	} else {
		target_id = id;
		snprintf(target_name, sizeof(target_name), "CBRAIN_%u", (unsigned)id);
	}
}

/* ---- 상태 LED (PCA10059 녹색 LD1) — 디버거 없는 동글의 육안 상태표시 ----
 * 스캔/연결중 = 깜빡, 브리지 준비(구독+control 확보) = 켜짐. */
static const struct gpio_dt_spec status_led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

static void led_tick(struct k_timer *t)
{
	ARG_UNUSED(t);
	if (control_handle) {
		gpio_pin_set_dt(&status_led, 1);   /* 연결·스트리밍: 켜짐 */
	} else if (target_id == 0 && !scanning) {
		gpio_pin_set_dt(&status_led, 0);   /* 미페어링(대기): 꺼짐 */
	} else {
		gpio_pin_toggle_dt(&status_led);   /* 지정 기기 탐색 중: 깜빡 */
	}
}
K_TIMER_DEFINE(led_timer, led_tick, NULL);

static void start_scan(void);

/* 스캔 재시작 / SET_TARGET 처리는 항상 시스템 워크큐에서 실행한다 — BT 콜백(disconnected)이나
 * down_thread(작은 스택) 컨텍스트에서 직접 bt_le_scan_start 하거나 flash(settings)를 쓰면
 * 실패/스택부족이 날 수 있다(Zephyr). 안전한 컨텍스트로 지연시키는 표준 패턴. */
static void device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
			 struct net_buf_simple *ad);
static struct k_work_delayable rescan_dwork;
static int last_scan_err;               /* 계측: 마지막 scan 시작 결과 */
static uint16_t adv_seen, match_seen;   /* 계측: 연결가능 광고 수신 수 / 이름매칭 수 */
static uint8_t conn_attempts;           /* 계측: bt_conn_le_create 시도 수 */
static int8_t last_conn_err;            /* 계측: 마지막 create 결과 */

static void rescan_work_fn(struct k_work *w)
{
	ARG_UNUSED(w);
	if (cb_conn || scanning) {
		return;                        /* 이미 연결/연결중 */
	}
	(void)bt_le_scan_stop();               /* 이전 타깃 스캔 중지(있으면) */
	if (target_id == 0) {
		LOG_INF("대기 — 타깃 미지정 (앱에서 페어링 필요)");
		return;                        /* 미페어링: 스캔조차 하지 않음 */
	}
	int err = bt_le_scan_start(BT_LE_SCAN_ACTIVE, device_found);
	last_scan_err = err;
	if (err && err != -EALREADY) {
		/* disconnect 직후 conn 리소스 미해제 등으로 실패 → 잠시 뒤 재시도(견고) */
		LOG_WRN("scan 재시작 실패(%d) — 300ms 뒤 재시도", err);
		(void)k_work_reschedule(&rescan_dwork, K_MSEC(300));
	} else {
		LOG_INF("스캔 중 — 대상 '%s'", target_name);
	}
}

static uint32_t pending_target;
static void set_target_work_fn(struct k_work *w)
{
	ARG_UNUSED(w);
	/* RAM-only 타깃: flash 쓰기(BLE 라디오와 충돌) 안 함. 학생 앱이 페어링마다 SET_TARGET 재전송. */
	discovery_cancel();
	address_target = false;
	stored_target = pending_target;
	apply_target(pending_target);
	LOG_INF("SET_TARGET → '%s'", target_name);
	if (cb_conn) {
		(void)bt_conn_disconnect(cb_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
	} else {
		(void)k_work_reschedule(&rescan_dwork, K_NO_WAIT);
	}
}


/* UP: 헤드스테이지 stream notify → CDC */
static uint8_t notify_func(struct bt_conn *conn, struct bt_gatt_subscribe_params *params,
			   const void *data, uint16_t length)
{
	ARG_UNUSED(conn);
	if (!data) {                       /* 구독 해제 */
		params->value_handle = 0U;
		return BT_GATT_ITER_STOP;
	}
	if (framed_mode) {
		send_event(0x90, data, length);
	} else {
		cdc_tx_push(data, length);
	}
	return BT_GATT_ITER_CONTINUE;
}

/* 발견 상태기계: Stream char → 그 CCC(구독) → Control char(핸들 확보) */
static uint8_t discover_func(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			     struct bt_gatt_discover_params *params)
{
	int err;

	if (!attr) {
		LOG_WRN("discover 종료(대상 특성 못 찾음, step=%d)", disc_step);
		return BT_GATT_ITER_STOP;
	}

	if (disc_step == D_STREAM_CHR) {
		sub_params.value_handle = bt_gatt_attr_value_handle(attr);
		disc_params.uuid = BT_UUID_GATT_CCC; /* CCC is a 16-bit UUID. */
		disc_params.start_handle = attr->handle + 2;
		disc_params.type = BT_GATT_DISCOVER_DESCRIPTOR;
		disc_step = D_STREAM_CCC;
		err = bt_gatt_discover(conn, &disc_params);
		if (err) {
			LOG_ERR("CCC discover 실패 (%d)", err);
		}
		return BT_GATT_ITER_STOP;
	}

	if (disc_step == D_STREAM_CCC) {
		sub_params.notify = notify_func;
		sub_params.value = BT_GATT_CCC_NOTIFY;
		sub_params.ccc_handle = attr->handle;
		err = bt_gatt_subscribe(conn, &sub_params);
		if (err && err != -EALREADY) {
			LOG_ERR("subscribe 실패 (%d)", err);
		} else {
			LOG_INF("Stream 구독됨");
		}
		/* 이어서 Control 특성 핸들 확보 */
		memcpy(&disc_uuid, &cb_uuid_control, sizeof(disc_uuid));
		disc_params.uuid = &disc_uuid.uuid;
		disc_params.start_handle = 0x0001;
		disc_params.end_handle = 0xffff;
		disc_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
		disc_step = D_CONTROL_CHR;
		err = bt_gatt_discover(conn, &disc_params);
		if (err) {
			LOG_ERR("Control discover 실패 (%d)", err);
		}
		return BT_GATT_ITER_STOP;
	}

	/* D_CONTROL_CHR */
	control_handle = bt_gatt_attr_value_handle(attr);
	LOG_INF("브리지 준비 — control_handle=0x%04x", control_handle);
	discovery_link_event(2, 0);
	return BT_GATT_ITER_STOP;
}

static void gatt_discover(struct bt_conn *conn)
{
	disc_step = D_STREAM_CHR;
	memcpy(&disc_uuid, &cb_uuid_stream, sizeof(disc_uuid));
	disc_params.uuid = &disc_uuid.uuid;
	disc_params.func = discover_func;
	disc_params.start_handle = 0x0001;
	disc_params.end_handle = 0xffff;
	disc_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;

	int err = bt_gatt_discover(conn, &disc_params);
	if (err) {
		LOG_ERR("discover 시작 실패 (%d)", err);
	}
}

static struct bt_gatt_exchange_params mtu_params;

static void mtu_cb(struct bt_conn *conn, uint8_t err, struct bt_gatt_exchange_params *p)
{
	ARG_UNUSED(p);
	LOG_INF("MTU 교환 %s → %u", err ? "실패" : "OK", bt_gatt_get_mtu(conn));
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err) {
		LOG_WRN("연결 실패 (0x%02x) — 재스캔", err);
		discovery_link_event(0, err);
		bt_conn_unref(cb_conn);
		cb_conn = NULL;
		(void)k_work_reschedule(&rescan_dwork, K_NO_WAIT);
		return;
	}
	LOG_INF("연결됨 → '%s'", target_name);
	discovery_link_event(1, 0);

	/* 처리량: 큰 MTU(프레임당 노티 수↓). 2M PHY/데이터길이확장은 컨트롤러 auto. */
	mtu_params.func = mtu_cb;
	(void)bt_gatt_exchange_mtu(conn, &mtu_params);

	gatt_discover(conn);
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	ARG_UNUSED(conn);
	LOG_INF("끊김 (0x%02x) — 재스캔", reason);
	discovery_link_event(0, reason);
	control_handle = 0;
	if (cb_conn) {
		bt_conn_unref(cb_conn);
		cb_conn = NULL;
	}
	(void)k_work_reschedule(&rescan_dwork, K_NO_WAIT);   /* 콜백 컨텍스트에서 직접 scan 금지 → 워크큐 지연 */
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
};

static uint8_t adv_name[24];             /* 계측: 마지막으로 본 CBRAIN 광고 이름 */
static uint8_t adv_name_len;

/* 광고 파싱: 이름이 대상(각인 시 정확히, 미각인 시 "CBRAIN" 접두)과 맞나 */
static bool ad_name_match(struct bt_data *data, void *user)
{
	bool *match = user;

	if (data->type != BT_DATA_NAME_COMPLETE && data->type != BT_DATA_NAME_SHORTENED) {
		return true;
	}
	if (data->data_len >= 6 && memcmp(data->data, "CBRAIN", 6) == 0) {
		adv_name_len = MIN(data->data_len, sizeof(adv_name));   /* 계측: 실제 이름 기록 */
		memcpy(adv_name, data->data, adv_name_len);
	}
	/* 와일드카드(ANY): 아무 "CBRAIN_" 헤드스테이지에 연결(첫 발견).
	 * 특정 번호: 그 이름과 **정확히** 일치할 때만.
	 * 미지정(0): 아무데도 연결 안 함 — 남의 헤드스테이지 가로채기 방지(SET_TARGET 필요). */
	if (target_id == CB_TARGET_ANY) {
		if (data->data_len >= 7 && memcmp(data->data, "CBRAIN_", 7) == 0) {
			*match = true;
		}
	} else if (target_id != 0) {
		size_t tlen = strlen(target_name);
		if (data->data_len == tlen && memcmp(data->data, target_name, tlen) == 0) {
			*match = true;
		}
	}
	return false;        /* 이름 필드 봤으면 파싱 종료 */
}

static void device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
			 struct net_buf_simple *ad)
{
	if (cb_conn) {
		return;
	}
	if (type != BT_GAP_ADV_TYPE_ADV_IND && type != BT_GAP_ADV_TYPE_ADV_DIRECT_IND) {
		return;
	}
	adv_seen++;
	if (scanning) {
		discovery_observe(addr, rssi, ad);
		return; /* Discovery NEVER connects. */
	}
	if (address_target && bt_addr_le_cmp(addr, &selected_addr)) {
		return;
	}

	bool match = false;
	bt_data_parse(ad, ad_name_match, &match);
	if (!match) {
		return;
	}
	match_seen++;

	if (bt_le_scan_stop()) {
		return;
	}
	conn_attempts++;
	int err = bt_conn_le_create(addr, BT_CONN_LE_CREATE_CONN,
				    BT_LE_CONN_PARAM_DEFAULT, &cb_conn);
	last_conn_err = (int8_t)err;
	if (err) {
		LOG_ERR("conn_create 실패 (%d) — 재스캔", err);
		(void)k_work_reschedule(&rescan_dwork, K_NO_WAIT);
	}
}

static void start_scan(void)
{
	int err = bt_le_scan_start(BT_LE_SCAN_ACTIVE, device_found);
	if (err) {
		LOG_ERR("scan 시작 실패 (%d)", err);
	} else {
		LOG_INF("스캔 중 — 대상 '%s'", target_name);
	}
}

/* ---- 런타임 페어링 (호스트 → 동글, CDC down-path) ----
 * 브리지 제어 프레임: MAGIC(4) + cmd(1) + payload.  SET_TARGET = MAGIC 01 <u32 LE>.
 * MAGIC 안 맞는 바이트는 헤드스테이지 Control 로 passthrough → 투명 브리지 유지(fw_builder 등). */
static const uint8_t BR_MAGIC[4] = { 0xCB, 0x1D, 0x70, 0x2A };
#define BR_CMD_SET_TARGET 0x01
#define BR_CMD_GET_STATUS 0x02

/* 계측 응답: MAGIC + 0x82 + target(4 LE) + flags(1) + scan_err(1). 호스트 진단이 파싱. */
static void send_status(void)
{
	uint8_t s[17];
	s[0] = BR_MAGIC[0]; s[1] = BR_MAGIC[1]; s[2] = BR_MAGIC[2]; s[3] = BR_MAGIC[3];
	s[4] = 0x82;
	uint32_t t = target_id;
	s[5] = t; s[6] = t >> 8; s[7] = t >> 16; s[8] = t >> 24;
	s[9] = (uint8_t)((cb_conn ? 1 : 0) | (control_handle ? 2 : 0)
	                  | (scanning ? 4 : 0) | (address_target ? 8 : 0));
	s[10] = (uint8_t)last_scan_err;
	s[11] = adv_seen; s[12] = adv_seen >> 8;
	s[13] = match_seen; s[14] = match_seen >> 8;
	s[15] = conn_attempts;
	s[16] = (uint8_t)last_conn_err;
	if (framed_mode) {
		send_event(0x82, s + 5, sizeof(s) - 5);
		return;
	}
	cdc_tx_push(s, sizeof(s));

	/* 광고 이름 계측: MAGIC + 0x83 + len(1) + name */
	uint8_t nm[6 + sizeof(adv_name)];
	nm[0] = BR_MAGIC[0]; nm[1] = BR_MAGIC[1]; nm[2] = BR_MAGIC[2]; nm[3] = BR_MAGIC[3];
	nm[4] = 0x83;
	nm[5] = adv_name_len;
	memcpy(&nm[6], adv_name, adv_name_len);
	cdc_tx_push(nm, 6 + adv_name_len);
}

static void passthrough(const uint8_t *d, size_t n)
{
	if (n && cb_conn && control_handle) {
		(void)bt_gatt_write_without_response(cb_conn, control_handle, d, n, false);
	}
}

#include "discovery.inc"

/* DOWN: CDC 수신 → 브리지 제어 프레임 추출 + 나머지는 헤드스테이지로 passthrough. */
static void down_thread(void)
{
	static uint8_t st, match, cmd, need, got, payload[8];   /* 단일 스레드 → static OK */
	uint8_t buf[64], pass[128];
	while (1) {
		if (k_sem_take(&rx_sem, K_MSEC(500))) {
			st = match = got = need = 0; /* Abandon truncated commands. */
			continue;
		}
		uint32_t n;
		while ((n = ring_buf_get(&rx_ring, buf, sizeof(buf))) > 0) {
			size_t pl = 0;
			for (uint32_t i = 0; i < n; i++) {
				uint8_t b = buf[i];
				if (st == 0) {                  /* magic 스캔 */
					if (b == BR_MAGIC[match]) {
						if (++match == 4) { st = 1; match = 0; }
					} else {                /* 매치 깨짐 → 부분 magic 은 passthrough */
						for (uint8_t k = 0; k < match; k++) {
							pass[pl++] = BR_MAGIC[k];
						}
						match = (b == BR_MAGIC[0]) ? 1 : 0;
						if (match == 0) {
							pass[pl++] = b;
						}
					}
				} else if (st == 1) {           /* cmd */
					cmd = b;
					got = 0;
					need = cb_command_size(cmd);
					st = need ? 2 : 0;
					if (cmd == BR_CMD_GET_STATUS) {
						send_status();  /* 계측: 상태 즉시 응답 */
					} else if (!need) {
						queue_command(cmd, payload);
					}
				} else {                        /* payload */
					payload[got++] = b;
					if (got >= need) {
						queue_command(cmd, payload);
						st = 0;
					}
				}
				if (pl >= sizeof(pass) - 4) { passthrough(pass, pl); pl = 0; }
			}
			if (pl) {
				passthrough(pass, pl);
			}
		}
	}
}
K_THREAD_DEFINE(down_tid, 2048, down_thread, NULL, NULL, NULL, 7, 0, 0);

static void read_target(void)
{
 /* Only explicit factory-stamped/compile-time targets auto-connect.
  * Neutral distribution ignores historical settings/UICR values. */
 uint32_t id = CONFIG_CB_BRIDGE_TARGET_ID;
 if (!id) { id = cb_target_blob.target; }
 apply_target(id);
}

int main(void)
{
	cdc_dev = DEVICE_DT_GET(DT_NODELABEL(cdc_acm_uart0));
	if (!device_is_ready(cdc_dev)) {
		LOG_ERR("CDC 장치 미준비");
		return 0;
	}
	if (usb_enable(NULL)) {
		LOG_ERR("usb_enable 실패");
		return 0;
	}
	uart_irq_callback_set(cdc_dev, cdc_isr);
	uart_irq_rx_enable(cdc_dev);

	if (gpio_is_ready_dt(&status_led)) {
		gpio_pin_configure_dt(&status_led, GPIO_OUTPUT_INACTIVE);
		k_timer_start(&led_timer, K_MSEC(250), K_MSEC(250));
	}

	k_work_init_delayable(&rescan_dwork, rescan_work_fn);
	discovery_init();

	if (settings_subsys_init() == 0) {
		(void)settings_load();                 /* stored_target 복원 (런타임 페어링 값) */
	}
	read_target();

	if (bt_enable(NULL)) {
		LOG_ERR("bt_enable 실패");
		return 0;
	}
	LOG_INF("CB Bridge 시작 — 대상 '%s' (id=%u)", target_name, (unsigned)target_id);
	radio_ready = true;
	if (target_id != 0) { start_scan(); }
	return 0;
}
