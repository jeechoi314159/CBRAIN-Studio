"""DisplayBuffer — 다운샘플·LED 재현·표시필터. 저장 경로와 무관(표시 전용)."""
from __future__ import annotations

import numpy as np

import cbrain_studio.core.packet as pk
from cbrain_studio.app.display_buffer import (
    DisplayBuffer, LedRule, _any_downsample, _minmax_downsample, make_display_filter,
)
from cbrain_studio.hal.simulated import SimulatedTransport


def _blocks(device_id=1, n=40, ch=4, sr=1024, spc=32):
    dec = pk.Decoder()
    out = []
    for f in SimulatedTransport(device_id=device_id, channel_count=ch, sr_hz=sr, spc=spc).generate(n):
        out += dec.decode(f).blocks
    return out


def test_minmax_downsample_preserves_peaks():
    sig = np.array([[0, 5, -3, 2, 9, -1, 4, 0]], dtype=np.float32)
    lo, hi = _minmax_downsample(sig, 4)
    assert lo.shape == (1, 4) and hi.shape == (1, 4)
    assert hi.max() == 9 and lo.min() == -3        # peak 이 다운샘플에서도 살아있음


def test_any_downsample_or():
    led = np.array([[False, False, True, False, False, False, True, False]])
    out = _any_downsample(led, 4)
    assert out.shape == (1, 4)
    assert out[0, 1] and out[0, 3]                 # True 가 있던 구간


def test_push_and_snapshot_no_deadlock():
    # 회귀: push 가 락 안에서 set_channels/_recent 를 재호출해 데드락 났던 버그.
    db = DisplayBuffer(sr_hz=1024, max_seconds=5)
    for b in _blocks(n=40):
        db.push(b)                                 # 무한루프면 여기서 멈춤
    s = db.snapshot(1.0, 200)
    assert s is not None and s["ch"] == 4 and s["lo"].shape == (4, 200)
    assert s["hi"].max() > s["lo"].min()


def test_led_reproduction_runs():
    db = DisplayBuffer(sr_hz=1024, max_seconds=5)
    db.set_led_rules([LedRule(True, 0, 4.0, 8.0, 250, threshold=1.0)])
    for b in _blocks(n=40):
        db.push(b)
    s = db.snapshot(1.0, 100)
    assert s["led"].shape == (1, 100)              # LED 레인 1개


def test_ring_wraps():
    db = DisplayBuffer(sr_hz=1024, max_seconds=1)  # cap=1024
    for b in _blocks(n=100, spc=32):               # 3200 samples > cap → wrap
        db.push(b)
    assert db._n == db._cap
    s = db.snapshot(1.0, 128)
    assert s is not None and s["lo"].shape[1] == 128


def test_display_filter_optional():
    assert make_display_filter() is None                      # nothing selected → no filter
    assert make_display_filter(band=None, notch=None) is None


def test_real_led_from_flags_overrides_reproduction():
    from cbrain_studio.core.types import SampleBlock
    db = DisplayBuffer(sr_hz=1024, max_seconds=5)
    db.set_led_rules([LedRule(True, 0, 4.0, 8.0, 250, threshold=1.0)])  # 재현 규칙이 있어도
    ch, spc = 4, 32
    for i in range(20):
        db.push(SampleBlock(device_id=1, first_counter=i * spc, sr_hz=1024,
                            channels=np.zeros((ch, spc), dtype=np.int16),
                            order=list(range(ch)),
                            flags=pk.FLAG_LED_VALID | pk.FLAG_LED0))   # LED1 on, LED2 off
    s = db.snapshot(1.0, 100)
    assert s["led_real"] is True                # 실측 사용
    assert s["led"].shape == (2, 100)           # 2 레인 (규칙 1개가 아니라 실측 2개)
    assert s["led"][0].all() and not s["led"][1].any()


def test_no_led_flag_falls_back_to_reproduction():
    db = DisplayBuffer(sr_hz=1024, max_seconds=5)
    db.set_led_rules([LedRule(True, 0, 4.0, 8.0, 250, threshold=1.0)])
    for b in _blocks(n=40):                      # SimulatedTransport → flags=0 (LED 미보고)
        db.push(b)
    s = db.snapshot(1.0, 100)
    assert s["led_real"] is False and s["led"].shape == (1, 100)   # 재현 1레인
