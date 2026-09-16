"""
BleakTransport — 호스트 자체 BLE 라디오로 헤드스테이지에 직접 연결 (bleak).

동글 없이(pc-ble-driver 불필요) CB 서비스 `0xCB10` 에 연결해 Stream(`0xCB11`) notify
바이트를 frame_handler 로 흘려보낸다. 커맨드는 Control(`0xCB12`) write.

동글 경로(hal/dongle.py, 프로덕션 타깃)와 **동일한 Transport 계약** — 데스크톱 코드 불변.
이 맥에서는 pc-ble-driver-py 가 Python 3.12 와 비호환이라 검증에 이 경로를 쓴다.
macOS: 실행 프로세스(터미널)에 블루투스 권한 필요.
"""
from __future__ import annotations

from bleak import BleakClient, BleakScanner

from .interfaces import RawFrameHandler, Transport

# CB v2 GATT (packet_protocol.md §3.1) — 128-bit, 소문자
CB_SERVICE = "01cbcb10-3412-109b-8a4b-e3aa8f52c3b1"
CB_STREAM = "01cbcb11-3412-109b-8a4b-e3aa8f52c3b1"    # notify (device→host)
CB_CONTROL = "01cbcb12-3412-109b-8a4b-e3aa8f52c3b1"   # write  (host→device)


class BleakTransport(Transport):
    """단일 헤드스테이지 ↔ 호스트 BLE. 이름 접두(CBRAIN) 또는 주소로 스캔·연결."""

    def __init__(self, name_prefix: str = "CBRAIN", address: str | None = None,
                 scan_timeout: float = 10.0):
        self.name_prefix = name_prefix
        self.address = address
        self.scan_timeout = scan_timeout
        self._client: BleakClient | None = None
        self._handler: RawFrameHandler | None = None
        self._open = False

    async def open(self) -> None:
        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=self.scan_timeout)
        else:
            def match(d, adv):
                name = adv.local_name or d.name or ""
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                return name.startswith(self.name_prefix) or CB_SERVICE in uuids
            dev = await BleakScanner.find_device_by_filter(match, timeout=self.scan_timeout)
        if dev is None:
            raise TimeoutError(f"'{self.name_prefix}…' / {CB_SERVICE} 미발견 "
                               f"({self.scan_timeout}s). 헤드스테이지 광고/전원 확인.")
        self._client = BleakClient(dev)
        await self._client.connect()
        await self._client.start_notify(CB_STREAM, self._on_notify)
        self._open = True

    def _on_notify(self, _sender, data: bytearray) -> None:
        if self._handler is not None:
            self._handler(bytes(data))          # 재조립은 Decoder 가 (§4.4)

    async def close(self) -> None:
        self._open = False
        if self._client is not None:
            try:
                await self._client.stop_notify(CB_STREAM)
            except Exception:  # noqa: BLE001
                pass
            await self._client.disconnect()

    async def send(self, frame: bytes) -> None:
        if self._client is None:
            raise RuntimeError("미연결 — open() 먼저")
        await self._client.write_gatt_char(CB_CONTROL, frame, response=False)

    def set_frame_handler(self, handler: RawFrameHandler) -> None:
        self._handler = handler

    @property
    def is_open(self) -> bool:
        return self._open
