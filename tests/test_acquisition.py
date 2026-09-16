"""
획득 파이프라인 — 무손실 불변식 + 갭→마커 + 디바이스당 1파일 HDF5 검증.

SimulatedTransport → AcquisitionManager → SampleBus → DeviceRecorder.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np

from cbrain_studio.app.acquisition import AcquisitionManager
from cbrain_studio.app.recording import DeviceRecorder, _HAS_H5PY
from cbrain_studio.core import packet
from cbrain_studio.hal.simulated import SimulatedTransport


def test_lossless_inmemory():
    """생성된 모든 샘플이 버스로 정확히 1회 전달되는지."""
    tx = SimulatedTransport(device_id=1, channel_count=4, sr_hz=1024, spc=32, mtu=40, seed=1)
    frames = tx.generate(20)                       # ground truth 프레임 바이트

    # ground truth: 독립 디코드
    gt = packet.Decoder()
    gt_blocks = []
    for f in frames:
        gt_blocks += gt.decode(f).blocks
    gt_samples = np.concatenate([b.channels for b in gt_blocks], axis=1)

    # 파이프라인
    acq = AcquisitionManager(tx)
    got = []
    acq.bus.subscribe(lambda blk: got.append(blk))
    acq.start()
    for f in frames:
        for i in range(0, len(f), 40):             # MTU 조각화
            acq._on_bytes(f[i:i + 40])
    got_samples = np.concatenate([b.channels for b in got], axis=1)

    assert acq.blocks_out == len(gt_blocks) == 20
    assert got_samples.shape == gt_samples.shape
    assert np.array_equal(got_samples, gt_samples)   # 정확히 1회, 손실/중복 없음
    assert acq.gaps == 0


def test_counter_gap_becomes_marker():
    """전송 누락(카운터 갭)이 조용히 사라지지 않고 discontinuity 로 감지되는지."""
    tx = SimulatedTransport(device_id=1, channel_count=2, sr_hz=1024, spc=32,
                            mtu=200, seed=2, drop_frames={3})   # seq=3 프레임 누락
    frames = tx.generate(10)
    acq = AcquisitionManager(tx)
    disc = []
    acq.on_discontinuity = lambda dev, cnt, t, missing: disc.append((dev, cnt, missing))
    acq.start()
    for f in frames:
        acq._on_bytes(f)
    assert acq.gaps == 1 and len(disc) == 1
    assert disc[0][2] == 32          # 누락 샘플 수 = spc


def test_device_file_hdf5():
    if not _HAS_H5PY:
        print("skip: h5py 미설치")
        return
    import h5py
    tx = SimulatedTransport(device_id=7, channel_count=8, sr_hz=1024, spc=32, mtu=180, seed=3)
    frames = tx.generate(15)
    acq = AcquisitionManager(tx)

    with tempfile.TemporaryDirectory() as d:
        rec = DeviceRecorder(d, device_id=7, metadata={"experiment": "sim", "animal_id": "M1"})

        def on_block(blk):
            if rec.channel_count is None:
                rec.open(blk.channel_count, blk.sr_hz, blk.order)
            tb = acq.timebase(blk.device_id)
            counters = list(range(blk.first_counter, blk.first_counter + blk.n_samples))
            ts = tb.sample_times(blk.first_counter, blk.n_samples)
            rec.write_block(blk, counters, ts)

        acq.bus.subscribe(on_block)
        acq.start()
        for f in frames:
            acq._on_bytes(f)
        path = rec.close()

        assert os.path.basename(path).startswith("Device_007_")
        with h5py.File(path, "r") as h5:
            assert h5.attrs["channel_count"] == 8
            assert h5.attrs["n_samples"] == 15 * 32
            assert h5["samples"].shape == (8, 15 * 32)
            assert h5["sample_counter"].shape == (15 * 32,)
            assert h5.attrs["experiment"] == "sim"
    print("OK hdf5 device file")


if __name__ == "__main__":
    test_lossless_inmemory(); print("OK lossless_inmemory")
    test_counter_gap_becomes_marker(); print("OK counter_gap_becomes_marker")
    test_device_file_hdf5()
    print("\n모든 acquisition 테스트 통과")
