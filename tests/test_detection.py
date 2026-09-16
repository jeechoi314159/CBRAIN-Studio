"""Event Detection — 알려진 임펄스 위치 대조 (하드웨어 없이)."""
from __future__ import annotations

from cbrain_studio.app.calibration import CalibrationManager
from cbrain_studio.app.detection import BandPowerDetector, ThresholdDetector, band_power
from cbrain_studio.core.types import SampleBlock
from cbrain_studio.sim.signal import impulse_counters, synth_block

SR, CH, SPC, N = 1024, 4, 16, 64


def _blocks(impulses):
    for i in range(N):
        c0 = i * SPC
        yield SampleBlock(device_id=1, first_counter=c0, sr_hz=SR,
                          channels=synth_block(1, c0, SPC, CH, SR, impulses),
                          order=list(range(CH)))


def _calibrate():
    cm = CalibrationManager(k=3.0)
    for blk in _blocks(impulses=False):     # 깨끗한 baseline
        cm.add_block(blk)
    return cm.result()


def test_detects_exactly_the_impulses():
    """임펄스(ch0, 512샘플 주기)를 정확히 검출하고 오검출 없음."""
    det = ThresholdDetector(_calibrate().threshold, refractory=32)
    markers = []
    for blk in _blocks(impulses=True):
        markers += det.process(blk)

    detected = sorted(m.counter for m in markers)
    assert detected == impulse_counters(N * SPC)     # 정확히 임펄스 위치
    assert all(m.value == 0 for m in markers)        # ch0 에서만 (value=채널)
    assert all(m.code == 4 for m in markers)         # EVENT 코드


def test_no_events_on_clean_baseline():
    """임펄스 없는 baseline 에선 이벤트 0 (sine 이 threshold 를 넘지 않음)."""
    det = ThresholdDetector(_calibrate().threshold, refractory=32)
    markers = []
    for blk in _blocks(impulses=False):
        markers += det.process(blk)
    assert markers == []


def test_band_power_identifies_channel_frequency():
    """FFT 대역 파워가 채널별 sine 주파수를 정확히 잡는다 (ch0=5Hz, ch1=8Hz).
    1024샘플 → 1Hz/bin 해상도로 5·8Hz 분리."""
    x = synth_block(1, 0, 1024, CH, SR, impulses=False)     # (CH, 1024)
    assert band_power(x[0], SR, 4, 6) > 10 * band_power(x[0], SR, 7, 9)   # ch0 = 5Hz
    assert band_power(x[1], SR, 7, 9) > 10 * band_power(x[1], SR, 4, 6)   # ch1 = 8Hz


def test_bandpower_detector_is_pluggable():
    """다른 알고리즘(BandPowerDetector)도 같은 Detector 계약으로 동작.
    [4,6]Hz 대역 검출기는 ch0(5Hz)에서만 발화, ch1(8Hz)에선 안 함."""
    win = 256
    x = synth_block(1, 0, win, CH, SR, impulses=False)
    p_in, p_out = band_power(x[0], SR, 4, 6), band_power(x[1], SR, 4, 6)
    threshold = (p_in + p_out) / 2                          # ch0 위 · ch1 아래

    det = BandPowerDetector(4, 6, SR, window=win, threshold=threshold, refractory=win)
    fired = set()
    for blk in _blocks(impulses=False):
        for m in det.process(blk):
            fired.add(m.value)
    assert 0 in fired            # ch0 (5Hz, 대역 안)
    assert 1 not in fired        # ch1 (8Hz, 대역 밖)
