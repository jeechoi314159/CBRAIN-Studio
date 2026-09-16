"""시뮬레이션 하네스 — 전 파이프라인 자동 검증 (하드웨어 없이)."""
from __future__ import annotations

import tempfile

from cbrain_studio.sim.harness import SPC, run_backpressure, run_device, verify


def _assert_all(path, device_id, kept, gap_at=None, ch_count=4, sync=None):
    for name, ok, detail in verify(path, device_id, kept, gap_at, ch_count, sync):
        assert ok, f"{name}: {detail}"


def test_pipeline_lossless_multidevice():
    """디바이스당 파일 분리 + 무손실 + 카운터/메타데이터."""
    with tempfile.TemporaryDirectory() as d:
        p1, kept1 = run_device(1, 20, d, None)
        p2, kept2 = run_device(2, 20, d, None)
        assert p1 != p2
        _assert_all(p1, 1, kept1, None)
        _assert_all(p2, 2, kept2, None)


def test_gap_becomes_discontinuity_marker():
    """프레임 누락 → discontinuity 마커로 기록(조용한 손실 없음)."""
    with tempfile.TemporaryDirectory() as d:
        path, kept = run_device(2, 20, d, gap_at=10)
        assert 10 not in kept
        _assert_all(path, 2, kept, gap_at=10)


def test_variable_channel_count():
    """1–16 채널 어느 것이든 무손실 (채널 하드코딩 없음)."""
    with tempfile.TemporaryDirectory() as d:
        for ch in (1, 8, 16):
            path, kept = run_device(90 + ch, 8, d, None, ch_count=ch)
            _assert_all(path, 90 + ch, kept, ch_count=ch)


def test_sync_events_recorded():
    """동기 이벤트(TTL/IR 소프트웨어 목)가 파일에 기록되는지."""
    with tempfile.TemporaryDirectory() as d:
        syncs = [5 * SPC, 15 * SPC]
        path, kept = run_device(1, 20, d, None, sync_counters=syncs)
        _assert_all(path, 1, kept, sync=syncs)


def test_backpressure_recording_stays_lossless():
    """느린 소비자는 자기 뷰만 버리고, 기록은 무손실."""
    with tempfile.TemporaryDirectory() as d:
        recorded, expected, dropped = run_backpressure(d, n_blocks=40)
        assert recorded == expected      # 기록 무손실
        assert dropped > 0               # lossy 소비자는 뷰 손실
