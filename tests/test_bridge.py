"""
BridgeTransport (동글→USB-CDC 경로) 테스트 — 하드웨어/pyserial 없이.

한 동글의 CDC 스트림에 **여러 CBRAIN 프레임이 원자적으로 섞여** 들어와도, 기존 Decoder 가
device_id 로 demux 해 블록을 뽑아내는지 검증한다(= 다중 기기 수신의 호스트측 핵심).
"""
from __future__ import annotations

import asyncio
import threading
import time

from cbrain_studio.app.acquisition import AcquisitionManager
from cbrain_studio.hal.bridge import BridgeTransport
from cbrain_studio.hal.simulated import SimulatedTransport


class FakeSerial:
    """BridgeTransport 의 open_fn seam 용 — read()/write()/close()/in_waiting."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, n: int) -> bytes:
        with self._lock:
            if self._buf:
                chunk = bytes(self._buf[:n])
                del self._buf[:n]
                return chunk
        time.sleep(0.002)          # 빈 버퍼 = timeout 흉내
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        pass


def _interleaved_stream(n_each: int = 5):
    """dev 2(4ch@1024) 와 dev 7(8ch@512) 의 완결 프레임을 번갈아 이어붙인 CDC 스트림."""
    sa = SimulatedTransport(device_id=2, channel_count=4, sr_hz=1024, spc=32)
    sb = SimulatedTransport(device_id=7, channel_count=8, sr_hz=512, spc=16)
    fa, fb = sa.generate(n_each), sb.generate(n_each)
    frames = []
    for a, b in zip(fa, fb):
        frames.append(a)          # 프레임 단위로 원자적 — 바이트 인터리브 없음
        frames.append(b)
    return b"".join(frames)


def test_bridge_demux_multidevice():
    stream = _interleaved_stream(5)
    fake = FakeSerial()
    bridge = BridgeTransport("fake", open_fn=lambda: fake)

    blocks = []
    acq = AcquisitionManager(bridge)
    acq.start()
    acq.bus.subscribe(blocks.append)

    async def run():
        await bridge.open()
        # 여러 조각으로 흘려 스레드 수신 경로를 실제로 태운다
        for i in range(0, len(stream), 40):
            fake.feed(stream[i:i + 40])
            await asyncio.sleep(0.003)
        for _ in range(100):       # 최대 ~1s 처리 대기
            if len(blocks) >= 10:
                break
            await asyncio.sleep(0.01)
        await bridge.close()

    asyncio.run(run())

    ids = sorted({b.device_id for b in blocks})
    assert ids == [2, 7], f"device demux 실패: {ids}"
    assert sum(b.device_id == 2 for b in blocks) == 5
    assert sum(b.device_id == 7 for b in blocks) == 5
    # 기기별 채널수/샘플레이트가 섞이지 않고 보존됐는지
    assert all(b.channel_count == 4 and b.sr_hz == 1024 for b in blocks if b.device_id == 2)
    assert all(b.channel_count == 8 and b.sr_hz == 512 for b in blocks if b.device_id == 7)


def test_bridge_open_close_state():
    fake = FakeSerial()
    bridge = BridgeTransport("fake", open_fn=lambda: fake)
    assert not bridge.is_open

    async def run():
        await bridge.open()
        assert bridge.is_open
        await bridge.send(b"\x43\x42ping")   # write 경로 (예외 안 나면 OK)
        await bridge.close()
        assert not bridge.is_open

    asyncio.run(run())
