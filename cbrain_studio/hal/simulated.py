"""
SimulatedTransport / SimulatedDevice — 하드웨어 없이 전체 파이프라인 테스트.

알려진 합성 신호(사인+노이즈)를 CB v2 DATA 프레임으로 인코딩해 프레임 핸들러로
흘려보낸다. 카운터 갭·동기 펄스를 주입할 수 있어 무손실/갭감지 테스트에 사용.
프레임은 MTU 흉내로 임의 크기 조각으로 쪼개 전송한다.
"""
from __future__ import annotations

import asyncio

import numpy as np

from cbrain_studio.core import packet
from cbrain_studio.core.types import DeviceCapabilities, SyncEvent

from .interfaces import Device, LedController, RawFrameHandler, SyncHandler, SyncSource, Transport


class SimulatedTransport(Transport):
    def __init__(self, device_id: int = 1, channel_count: int = 4, sr_hz: int = 1024,
                 spc: int = 32, mtu: int = 180, seed: int = 0,
                 drop_frames: set[int] | None = None):
        self._device_id = device_id
        self._ch = channel_count
        self._sr = sr_hz
        self._spc = spc
        self._mtu = mtu
        self._rng = np.random.default_rng(seed)
        self._drop = drop_frames or set()      # 이 seq 의 DATA 프레임은 '전송 누락'(갭 유발)
        self._handler: RawFrameHandler | None = None
        self._task: asyncio.Task | None = None
        self._open = False
        self._seq = 0
        self._counter = 0

    # ── Transport API ──
    async def open(self) -> None:
        self._open = True

    async def close(self) -> None:
        self._open = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def send(self, frame: bytes) -> None:
        pass  # 시뮬레이터는 커맨드를 수용만 (Phase1)

    def set_frame_handler(self, handler: RawFrameHandler) -> None:
        self._handler = handler

    @property
    def is_open(self) -> bool:
        return self._open

    # ── 스트림 제어 ──
    async def start_stream(self, n_frames: int | None = None, interval: float = 0.0) -> None:
        self._task = asyncio.ensure_future(self._run(n_frames, interval))

    async def _run(self, n_frames: int | None, interval: float) -> None:
        produced = 0
        while self._open and (n_frames is None or produced < n_frames):
            frame = self._make_data_frame()
            produced += 1
            if self._seq - 1 not in self._drop:      # drop 이면 전송 안 함 → 카운터 갭
                self._emit(frame)
            if interval:
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(0)

    def _make_data_frame(self) -> bytes:
        ch, spc = self._ch, self._spc
        t = (np.arange(spc) + self._counter) / self._sr
        sig = np.zeros((ch, spc), dtype=np.int16)
        for c in range(ch):
            wave = 8000 * np.sin(2 * np.pi * (5 + 3 * c) * t)
            noise = self._rng.normal(0, 300, spc)
            sig[c] = np.clip(wave + noise, -32768, 32767).astype(np.int16)
        seq = self._seq
        frame = packet.encode_data(
            seq=seq, device_id=self._device_id, channels=sig,
            first_counter=self._counter & 0xFFFFFFFF, sr_hz=self._sr,
            order=list(range(ch)), t0_tick=self._counter * 32, tick_hz=32768)
        self._seq += 1
        self._counter += spc
        return frame

    def _emit(self, frame: bytes) -> None:
        if not self._handler:
            return
        # MTU 흉내: 조각으로 쪼개 전달
        for i in range(0, len(frame), self._mtu):
            self._handler(frame[i:i + self._mtu])

    # 테스트 편의: 동기 방식으로 N 프레임 바이트 생성(핸들러 없이)
    def generate(self, n_frames: int) -> list[bytes]:
        out = []
        for _ in range(n_frames):
            f = self._make_data_frame()
            if self._seq - 1 not in self._drop:
                out.append(f)
        return out


class SimulatedDevice(Device):
    def __init__(self, transport: SimulatedTransport, caps: DeviceCapabilities):
        self._t = transport
        self._caps = caps

    @property
    def device_id(self) -> int:
        return self._caps.device_id

    async def capabilities(self) -> DeviceCapabilities:
        return self._caps

    async def start_stream(self) -> None:
        await self._t.start_stream()

    async def stop_stream(self) -> None:
        await self._t.close()


class SimulatedLed(LedController):
    def __init__(self, count: int = 2):
        self._count = count
        self.state = [0] * count

    @property
    def led_count(self) -> int:
        return self._count

    async def set_led(self, index: int, rgb_mask: int) -> None:
        if 0 <= index < self._count:
            self.state[index] = rgb_mask


class SimulatedSyncSource(SyncSource):
    def __init__(self, source_id: str = "ir"):
        self._id = source_id
        self._handler: SyncHandler | None = None

    @property
    def source_id(self) -> str:
        return self._id

    def set_handler(self, handler: SyncHandler) -> None:
        self._handler = handler

    def pulse(self, counter: int, host_time: float, edge: int = 1) -> None:
        if self._handler:
            self._handler(SyncEvent(self._id, counter, host_time, edge))
