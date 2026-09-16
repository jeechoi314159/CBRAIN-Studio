"""Calibration — 알려진 baseline 통계 대조 (하드웨어 없이)."""
from __future__ import annotations

import numpy as np

from cbrain_studio.app.calibration import CalibrationManager
from cbrain_studio.core.types import SampleBlock
from cbrain_studio.sim.signal import synth_block

SR, CH, SPC, N = 1024, 4, 16, 64


def _blocks(impulses):
    for i in range(N):
        c0 = i * SPC
        yield SampleBlock(device_id=1, first_counter=c0, sr_hz=SR,
                          channels=synth_block(1, c0, SPC, CH, SR, impulses),
                          order=list(range(CH)))


def test_calibration_mean_sd_threshold():
    """계산된 mean/SD 가 baseline 의 실제 통계와 일치, threshold = mean + k·SD."""
    cm = CalibrationManager(k=3.0)
    allsamp = []
    for blk in _blocks(impulses=False):
        cm.add_block(blk)
        allsamp.append(blk.channels)
    exp = np.concatenate(allsamp, axis=1).astype(np.float64)     # (ch, N*SPC)

    r = cm.result()
    assert r.channel_count == CH and r.n_samples == N * SPC
    assert np.allclose(r.mean, exp.mean(axis=1), atol=1e-6)
    assert np.allclose(r.sd, exp.std(axis=1), atol=1e-6)          # 모집단 std
    assert np.allclose(r.threshold, r.mean + 3.0 * r.sd)


def test_calibration_channel_agnostic():
    """채널 수는 블록에서 읽어 적응 (1–16)."""
    for ch in (1, 8, 16):
        cm = CalibrationManager()
        cm.add_block(SampleBlock(1, 0, SR, synth_block(1, 0, SPC, ch, SR, False),
                                 list(range(ch))))
        assert cm.result().channel_count == ch
