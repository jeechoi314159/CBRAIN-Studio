"""
BridgeTransport — nRF52840 'cb_bridge' 동글(커스텀 central fw) → USB-CDC → 호스트.

동글이 여러 CBRAIN 에 BLE central 로 붙어, 각 CB 프레임을 USB-CDC 로 **원자적으로**(프레임
단위로 끊기지 않게) 흘린다. CB 프레임은 자기구분(magic 'CB' + len + CRC16)이라, CDC 바이트
스트림에서 기존 Decoder 가 그대로 재조립하고 device_id 로 demux 한다 — BLE 경로와 완전히 동일.

pc-ble-driver 불필요 → pyserial 만 있으면 **어떤 Python 3 에서나** 동작(0.11.4 의 Py2.7 네이티브
바이너리 벽을 우회). 동글이 여러 개면 포트별 BridgeTransport 인스턴스를 각자 AcquisitionManager
에 물려 하나의 SampleBus 로 합류시킨다(device_id 라우팅은 기존 파이프라인이 담당).

pyserial 은 지연 import — 라이브러리 없이도 코어/시뮬은 그대로 import 된다.
"""
from __future__ import annotations

import threading

from .interfaces import RawFrameHandler, Transport

# 브리지 제어 (host → dongle CDC): MAGIC(4) + cmd(1) + payload. firmware cb_set_target 와 계약.
_BR_MAGIC = bytes([0xCB, 0x1D, 0x70, 0x2A])
_BR_CMD_SET_TARGET = 0x01

TARGET_ANY = 0xFFFFFFFF   # SET_TARGET 와일드카드: 아무 CBRAIN_* 헤드스테이지에 자동 연결


def set_target_frame(target: int) -> bytes:
    """동글에 '대상 CBRAIN 번호'를 각인(flash 저장)하는 CDC 프레임 → CBRAIN_<target> 에만 연결."""
    return _BR_MAGIC + bytes([_BR_CMD_SET_TARGET]) + int(target).to_bytes(4, "little")


def find_bridge_ports() -> list[str]:
    """연결된 cb_bridge 동글의 CDC 포트들 (VID 0x1915 / PID 0x520F). 데이터 포트만."""
    from serial.tools import list_ports
    return sorted(p.device for p in list_ports.comports()
                  if p.vid == 0x1915 and p.pid == 0x520F)


class BridgeTransport(Transport):
    """단일 동글(CDC 포트) ↔ 호스트. 여러 동글 = 인스턴스 여러 개."""

    def __init__(self, serial_port: str, baud_rate: int = 1_000_000,
                 read_chunk: int = 4096, open_fn=None):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self._read_chunk = read_chunk
        self._open_fn = open_fn          # 테스트 seam: read()/write()/close()/in_waiting 제공 객체 반환

        self._handler: RawFrameHandler | None = None
        self._ser = None
        self._open = False
        self._reader: threading.Thread | None = None

    # ── Transport API ─────────────────────────────────────────
    async def open(self) -> None:
        if self._open_fn is not None:
            self._ser = self._open_fn()
        else:
            import serial  # 지연 import (pyserial)
            self._ser = serial.Serial(self.serial_port, self.baud_rate, timeout=0.1)
        self._open = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"bridge-rx:{self.serial_port}")
        self._reader.start()

    async def close(self) -> None:
        self._open = False
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001 — 종료 경로는 최선 노력
                pass

    async def send(self, frame: bytes) -> None:
        """호스트 → 동글 (→ 대상 링크의 Control 특성으로 라우팅). CB COMMAND 프레임 write."""
        if self._ser is None:
            raise RuntimeError("bridge 미연결 — open() 먼저 호출")
        self._ser.write(frame)

    def set_frame_handler(self, handler: RawFrameHandler) -> None:
        self._handler = handler

    @property
    def is_open(self) -> bool:
        return self._open

    # ── CDC 수신 루프 (백그라운드 스레드) ─────────────────────
    def _read_loop(self) -> None:
        ser = self._ser
        while self._open and ser is not None:
            try:
                n = getattr(ser, "in_waiting", 0)
                data = ser.read(n if n else 1)     # timeout 시 b'' → 루프 재확인(반응성)
            except Exception:  # noqa: BLE001 — 포트 분리/종료 시 탈출
                break
            if data and self._handler is not None:
                self._handler(bytes(data))         # 재조립/demux 는 Decoder 가
