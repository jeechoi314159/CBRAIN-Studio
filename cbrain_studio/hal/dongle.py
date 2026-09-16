"""
DongleTransport — nRF52840 USB 동글(connectivity fw) 기반 실 BLE Transport.

hal.interfaces.Transport 구현. 헤드스테이지 CB 서비스(0xCB10)에 연결해
Stream 특성(0xCB11, Notify) 알림 바이트를 그대로 frame_handler 로 흘려보낸다.
커맨드는 Control 특성(0xCB12, Write)으로 보낸다. (packet_protocol.md §3.1/§3.2)

pc-ble-driver-py 는 **지연 import** — 라이브러리 없이도 앱의 나머지(코어/시뮬)는 동작.
Apple Silicon 에서는 x86_64 파이썬 환경 필요(bringup.md '동글 경로' 참고).

⚠ 하드웨어 미검증. 실장비에서 확인할 [OPEN] 항목:
    · UUID 재사용 vs 신규 base (packet_protocol §P1)
    · connectivity handle → device_id 매핑 (packet_protocol §P4)
    · connectivity fw 의 att_mtu / baud
"""
from __future__ import annotations

import threading

from .interfaces import RawFrameHandler, Transport

# CB v2 GATT (packet_protocol.md §3.1). vendor base = 01CB0000-3412-109B-8A4B-E3AA8F52C3B1
CB_BASE = "01CB00003412109B8A4BE3AA8F52C3B1"   # 16-byte base, MSB-first, no dashes
CB_SERVICE = 0xCB10
CB_STREAM = 0xCB11    # device → host, Notify
CB_CONTROL = 0xCB12   # host → device, Write


class DongleTransport(Transport):
    """단일 헤드스테이지 ↔ 동글 ↔ 호스트. (멀티 디바이스는 handle 별 인스턴스로 확장)"""

    def __init__(self, serial_port: str, baud_rate: int = 1_000_000,
                 name_prefix: str = "CB", scan_timeout_s: float = 10.0):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.name_prefix = name_prefix
        self.scan_timeout_s = scan_timeout_s

        self._handler: RawFrameHandler | None = None
        self._driver = None          # BLEDriver
        self._adapter = None         # BLEAdapter
        self._base_uuid = None       # BLEUUIDBase
        self._conn_handle: int | None = None
        self._connected = threading.Event()
        self._open = False

    # ── Transport API ─────────────────────────────────────────
    async def open(self) -> None:
        self._start_driver()
        self._scan_and_connect()          # blocking; fills self._conn_handle
        self._adapter.service_discovery(self._conn_handle)
        self._enable_stream_notifications()
        self._open = True

    async def close(self) -> None:
        self._open = False
        try:
            if self._conn_handle is not None and self._driver is not None:
                self._driver.ble_gap_disconnect(self._conn_handle)
        except Exception:  # noqa: BLE001 — 종료 경로는 최선 노력
            pass
        finally:
            if self._driver is not None:
                self._driver.close()
            self._conn_handle = None

    async def send(self, frame: bytes) -> None:
        """Control 특성(0xCB12)에 CB v2 COMMAND 프레임 write."""
        if self._conn_handle is None or self._adapter is None:
            raise RuntimeError("dongle 미연결 — open() 먼저 호출")
        from pc_ble_driver_py.ble_driver import BLEUUID
        self._adapter.write_req(
            self._conn_handle, BLEUUID(CB_CONTROL, self._base_uuid), list(frame))

    def set_frame_handler(self, handler: RawFrameHandler) -> None:
        self._handler = handler

    @property
    def is_open(self) -> bool:
        return self._open

    # ── pc-ble-driver-py 연결 (지연 import) ────────────────────
    def _start_driver(self) -> None:
        from pc_ble_driver_py.ble_adapter import BLEAdapter
        from pc_ble_driver_py.ble_driver import BLEDriver, BLEUUIDBase

        self._driver = BLEDriver(
            serial_port=self.serial_port, baud_rate=self.baud_rate, auto_flash=False)
        self._adapter = BLEAdapter(self._driver)
        self._driver.observer_register(_DriverObserver(self))
        self._adapter.observer_register(_AdapterObserver(self))
        self._driver.open()
        self._driver.ble_enable()
        # 벤더 128-bit base 등록 → 이후 16-bit alias(0xCB10..)로 특성 참조
        self._base_uuid = BLEUUIDBase(bytes.fromhex(CB_BASE))
        self._driver.ble_vs_uuid_add(self._base_uuid)

    def _scan_and_connect(self) -> None:
        self._connected.clear()
        self._driver.ble_gap_scan_start()
        if not self._connected.wait(self.scan_timeout_s):
            raise TimeoutError(
                f"헤드스테이지 '{self.name_prefix}…' 광고 미탐지 "
                f"({self.scan_timeout_s}s). fw 광고/동글 connectivity 확인.")

    def _enable_stream_notifications(self) -> None:
        from pc_ble_driver_py.ble_driver import BLEUUID
        self._adapter.enable_notification(
            self._conn_handle, BLEUUID(CB_STREAM, self._base_uuid))

    # ── 관찰자 콜백에서 호출 ───────────────────────────────────
    def _on_adv(self, driver, peer_addr, name: str) -> None:
        if self._conn_handle is None and name.startswith(self.name_prefix):
            driver.ble_gap_scan_stop()
            self._adapter.connect(peer_addr)

    def _on_connected(self, conn_handle: int) -> None:
        self._conn_handle = conn_handle
        self._connected.set()

    def _on_notification(self, data: bytes) -> None:
        # 알림 바이트를 그대로 디코더로 (재조립은 Decoder 가; §4.4)
        if self._handler is not None:
            self._handler(bytes(data))


# pc-ble-driver-py 관찰자는 별도 클래스로(라이브러리 base 상속) — 지연 import 안에서 정의
def _DriverObserver(owner: DongleTransport):
    from pc_ble_driver_py.ble_driver import BLEAdvData
    from pc_ble_driver_py.observers import BLEDriverObserver

    class _Obs(BLEDriverObserver):
        def on_gap_evt_adv_report(self, driver, conn_handle, peer_addr,
                                  rssi, adv_type, adv_data):
            name = ""
            recs = adv_data.records
            for key in (BLEAdvData.Types.complete_local_name,
                        BLEAdvData.Types.short_local_name):
                if key in recs:
                    name = "".join(map(chr, recs[key]))
                    break
            owner._on_adv(driver, peer_addr, name)

        def on_gap_evt_connected(self, driver, conn_handle, peer_addr, role, conn_params):
            owner._on_connected(conn_handle)

    return _Obs()


def _AdapterObserver(owner: DongleTransport):
    from pc_ble_driver_py.observers import BLEAdapterObserver

    class _Obs(BLEAdapterObserver):
        def on_notification(self, adapter, conn_handle, uuid, data):
            owner._on_notification(data)

    return _Obs()
