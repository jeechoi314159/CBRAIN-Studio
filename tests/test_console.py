"""획득 콘솔(블럭화) — 기록·채널적응·frequency-to-lit 게이트 통합 검증 (하드웨어 없이)."""
from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
h5py = pytest.importorskip("h5py")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cbrain_studio.app.acquisition import AcquisitionManager  # noqa: E402
from cbrain_studio.app.led_gate import BandPowerGate  # noqa: E402
from cbrain_studio.core import packet  # noqa: E402
from cbrain_studio.hal.simulated import SimulatedTransport  # noqa: E402
from cbrain_studio.sim.signal import synth_block  # noqa: E402
from cbrain_studio.ui.console import SR, ConsoleWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wire(win):
    win.tx = SimulatedTransport(device_id=1)
    win.acq = AcquisitionManager(win.tx)
    win.acq.start()
    win.acq.bus.subscribe(win.scope.push)
    win.acq.bus.subscribe(win._consume)
    win.acq.on_discontinuity = win._on_disc
    win._begin_calibration()


def _feed(win, seq, c0, ch, impulses=False):
    x = synth_block(1, c0, 16, ch, SR, impulses)
    fr = packet.encode_data(seq, 1, x, c0, SR, order=list(range(ch)))
    for off in range(0, len(fr), 40):
        win.acq._on_bytes(fr[off:off + 40])


def test_records_with_metadata(qapp, tmp_path):
    win = ConsoleWindow(out_dir=str(tmp_path)); _wire(win)
    win.exp_edit.setText("cnv"); win.animal_edit.setText("R1")
    win._caps = type("C", (), {"fw": (0, 1, 0)})()
    win.rec_btn.setChecked(True)
    seq = c0 = 0
    for _ in range(30):
        _feed(win, seq, c0, 4); seq += 1; c0 += 16
    path = win._recorder.path
    assert win._recorder.n_written == 30 * 16
    win.rec_btn.setChecked(False); _feed(win, seq, c0, 4)
    assert win._recorder is None
    with h5py.File(path, "r") as f:
        assert f.attrs["experiment"] == "cnv" and f.attrs["animal_id"] == "R1"
        assert f.attrs["firmware_version"] == "0.1.0"
        assert f["samples"].shape == (4, 30 * 16)
    win.close()


def test_adapts_channel_count(qapp, tmp_path):
    win = ConsoleWindow(out_dir=str(tmp_path)); _wire(win)
    _feed(win, 0, 0, 2)
    assert win.scope.ch == 2 and win.phase == "calibrating"
    win.close()


def test_freq_to_lit_gate_marks_recording(qapp, tmp_path):
    """대역 신호가 나타나면 LED 게이트가 켜지고, 기록에 마커로 남는다."""
    win = ConsoleWindow(out_dir=str(tmp_path)); _wire(win)
    # 기록 시작
    win._caps = type("C", (), {"fw": (0, 1, 0)})()
    win.rec_btn.setChecked(True)
    _feed(win, 0, 0, 4)                          # recorder open

    # 준비된 게이트 주입: [8,12]Hz, 노이즈 baseline 로 캘리브
    win_n = int(SR * 0.25)
    gate = BandPowerGate(SR, 8.0, 12.0, win_n, k=3.0, channels=[0])
    rng = np.random.default_rng(0)
    for _ in range(40):
        gate.add_calib(rng.normal(0, 20, (4, win_n)))
    gate.finalize()
    win.gate = gate; win.phase = "detecting"

    t = np.arange(win_n) / SR
    # 대역 안 10Hz 강신호 → LED ON + 마커
    win.scope.buf[0, -win_n:] = 4000 * np.sin(2 * np.pi * 10 * t)
    win._gate_step()
    assert win.scope._led_state == 1
    # 잠잠 → LED OFF
    win.scope.buf[0, -win_n:] = rng.normal(0, 20, win_n)
    win._gate_step()
    assert win.scope._led_state == 0

    path = win._recorder.path
    win.rec_btn.setChecked(False); _feed(win, 1, 64, 4)
    with h5py.File(path, "r") as f:
        assert "event_markers" in f
        vals = list(f["event_markers"]["value"])
        assert 1 in vals and 0 in vals          # ON/OFF 마커
    win.close()
