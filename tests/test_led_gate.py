"""BandPowerGate — 대역 선택적 점등 로직 (frequency-to-lit)."""
from __future__ import annotations

import numpy as np

from cbrain_studio.app.led_gate import BandPowerGate

SR, WIN = 1024, 1024      # 1 Hz/bin 해상도로 대역 분리 명확


def _sine(f, amp, n=WIN):
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * f * t)).reshape(1, n)


def test_band_selective_gate():
    """[9,11]Hz 게이트: baseline(잔잔한 노이즈)로 캘리브레이션 후,
    대역 안 10Hz 는 점등, 대역 밖 20Hz 는 점등 안 함."""
    rng = np.random.default_rng(0)
    gate = BandPowerGate(SR, 9.0, 11.0, WIN, k=3.0, channels=[0])
    for _ in range(60):                       # baseline: 저진폭 노이즈
        gate.add_calib(rng.normal(0, 20, (1, WIN)))
    assert gate.finalize() and gate.ready

    assert gate.active(_sine(10, 3000))       # 대역 안 → ON
    assert not gate.active(_sine(20, 3000))   # 대역 밖 → OFF
    assert not gate.active(rng.normal(0, 20, (1, WIN)))   # 잔잔 → OFF


def test_not_ready_before_finalize():
    gate = BandPowerGate(SR, 9.0, 11.0, WIN)
    assert not gate.ready
    assert gate.active(_sine(10, 3000)) is False   # 미확정이면 항상 False


def test_short_window_ignored_in_calib():
    gate = BandPowerGate(SR, 9.0, 11.0, WIN)
    gate.add_calib(np.zeros((1, WIN // 2)))   # 윈도우 미달 → 무시
    assert not gate.finalize()                # 누적 없음
