"""SessionManager / DeviceLink — 하드웨어 없이 (FakeSerial + SimulatedTransport 프레임)."""
from __future__ import annotations

import threading
import time

from cbrain_studio.app.session import DeviceLink, LinkState, SessionManager
from cbrain_studio.hal.simulated import SimulatedTransport


class FakeSerial:
    def __init__(self, stream=b""):
        self._buf = bytearray(stream)
        self._lock = threading.Lock()

    def feed(self, data):
        with self._lock:
            self._buf.extend(data)

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._buf)

    def read(self, n):
        with self._lock:
            if self._buf:
                c = bytes(self._buf[:n]); del self._buf[:n]; return c
        time.sleep(0.002); return b""

    def write(self, data):   # SET_TARGET 등 — 수용만
        return len(data)

    def close(self):
        pass


def _frames(device_id, n, ch=4, sr=1024, spc=16):
    return b"".join(SimulatedTransport(device_id=device_id, channel_count=ch,
                                       sr_hz=sr, spc=spc).generate(n))


def _wait(cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_link_pairs_and_records(tmp_path):
    fake = FakeSerial()
    link = DeviceLink("fake", target_id=1)
    link.tx_factory = None
    # BridgeTransport 에 open_fn 주입을 위해 pair 를 살짝 우회
    from cbrain_studio.hal.bridge import BridgeTransport
    from cbrain_studio.app.acquisition import AcquisitionManager
    link.tx = BridgeTransport("fake", open_fn=lambda: fake)
    link.acq = AcquisitionManager(link.tx)
    link.acq.on_discontinuity = link._on_disc
    link.acq.start()
    link.acq.bus.subscribe(link._on_block)
    import asyncio; asyncio.new_event_loop().run_until_complete(link.tx.open())
    link.state = LinkState.PAIRING

    link.arm_recording(str(tmp_path))
    fake.feed(_frames(1, 6))                      # device_id 1 프레임

    assert _wait(lambda: link.state == LinkState.RECORDING), link.state
    assert link.device_id == 1
    assert not link.id_mismatch()
    assert link.stats.written_samples > 0

    path = link.stop_recording()
    assert path and path.endswith(".h5")
    link.close()


def test_id_mismatch_detected():
    link = DeviceLink("fake", target_id=2)
    from cbrain_studio.hal.bridge import BridgeTransport
    from cbrain_studio.app.acquisition import AcquisitionManager
    fake = FakeSerial()
    link.tx = BridgeTransport("fake", open_fn=lambda: fake)
    link.acq = AcquisitionManager(link.tx); link.acq.start()
    link.acq.bus.subscribe(link._on_block)
    import asyncio; asyncio.new_event_loop().run_until_complete(link.tx.open())
    link.state = LinkState.PAIRING
    fake.feed(_frames(1, 4))                      # 지정=2 인데 실제=1 로 들어옴
    assert _wait(lambda: link.device_id == 1)
    assert link.id_mismatch()                     # 사고 감지
    link.close()


def test_wildcard_never_mismatches():
    from cbrain_studio.hal.bridge import TARGET_ANY, BridgeTransport
    from cbrain_studio.app.acquisition import AcquisitionManager
    link = DeviceLink("fake", target_id=TARGET_ANY)
    assert link.is_wildcard
    fake = FakeSerial()
    link.tx = BridgeTransport("fake", open_fn=lambda: fake)
    link.acq = AcquisitionManager(link.tx); link.acq.start()
    link.acq.bus.subscribe(link._on_block)
    import asyncio; asyncio.new_event_loop().run_until_complete(link.tx.open())
    link.state = LinkState.PAIRING
    fake.feed(_frames(7, 4))                       # 아무 기기(7)로 연결 — 자동 감지
    assert _wait(lambda: link.device_id == 7)
    assert not link.id_mismatch()                 # 와일드카드: 불일치 개념 없음
    link.close()


def test_preflight_blocks_when_not_streaming():
    sm = SessionManager(out_root="/tmp/cbrain_test")
    link = sm.add("fake", target_id=1)            # not paired → IDLE
    rep = sm.preflight()
    assert not rep.ok
    assert any("streaming" in f for f in rep.failures)


def test_stale_after_no_data():
    link = DeviceLink("fake", target_id=1)
    link.state = LinkState.STREAMING
    link.stats.last_block_time = time.monotonic() - 5.0
    link.tick(time.monotonic())
    assert link.state == LinkState.STALE          # 경보만 (자동 재연결 안 함)
