"""
시뮬레이션 하네스 코어 — 알려진 신호로 전 파이프라인을 구동·검증한다.

디바이스당 파이프라인 1개(1 동글 : 1 헤드스테이지 모델):
  synth_block → CB 프레임 인코딩 → (MTU 조각) → AcquisitionManager → SampleBus
  → DeviceRecorder(디바이스당 HDF5).  이후 HDF5 를 ground truth 와 대조.

검증하는 아키텍처 불변식:
  - 무손실(기록==생성, 정확히 1회) · 카운터 연속성 · 갭→discontinuity 마커
  - 디바이스당 파일 분리(멀티 디바이스) · 메타데이터 · 타임스탬프 단조
  - 가변 채널 수 (1–16, 하드코딩 없음)
  - 동기 이벤트 기록 (SimulatedSyncSource = TTL/IR 소프트웨어 목)
  - 백프레셔: 느린(lossy) 소비자는 '자기 뷰'만 버리고 기록은 무손실

CLI 는 tools/sim_harness.py, 자동 검증은 tests/test_sim_harness.py.
"""
from __future__ import annotations

import h5py
import numpy as np

from cbrain_studio.app.acquisition import AcquisitionManager
from cbrain_studio.app.recording import DeviceRecorder
from cbrain_studio.core import packet
from cbrain_studio.core.ring_buffer import RingBuffer
from cbrain_studio.hal.simulated import SimulatedTransport
from cbrain_studio.sim.signal import expected_stream, synth_block

SR_HZ, CH, SPC, MTU = 1024, 4, 16, 40
DISC_CODE = 0xFF


def _feed(acq, frame):
    for off in range(0, len(frame), MTU):
        acq._on_bytes(frame[off:off + MTU])


def run_device(device_id: int, n_blocks: int, out_dir: str, gap_at: int | None = None,
               ch_count: int = CH, sync_counters: list[int] | None = None):
    """한 디바이스 파이프라인 실행 → (hdf5_path, kept_block_indices)."""
    sync_counters = sync_counters or []
    acq = AcquisitionManager(SimulatedTransport(device_id=device_id))
    acq.start()
    rec = DeviceRecorder(out_dir, device_id,
                         metadata={"experiment": "sim", "animal_id": f"M{device_id}"})

    def on_block(blk):
        if rec.channel_count is None:
            rec.open(blk.channel_count, blk.sr_hz, blk.order)
        tb = acq.timebase(blk.device_id)
        counters = range(blk.first_counter, blk.first_counter + blk.n_samples)
        ts = tb.sample_times(blk.first_counter, blk.n_samples)
        rec.write_block(blk, list(counters), ts)

    acq.bus.subscribe(on_block)
    acq.on_discontinuity = lambda dev, cnt, t, missing: rec.add_discontinuity(cnt, t, missing)
    acq.on_sync = rec.add_sync                       # TTL/IR (소프트웨어 목) → 기록

    kept, sj = [], 0
    for i in range(n_blocks):
        if i == gap_at:
            continue                                 # 프레임 누락 → 카운터 갭
        kept.append(i)
        block = synth_block(device_id, i * SPC, SPC, ch_count, SR_HZ)
        _feed(acq, packet.encode_data(seq=i, device_id=device_id, channels=block,
                                      first_counter=i * SPC, sr_hz=SR_HZ,
                                      order=list(range(ch_count))))
        # 이 블록 구간에 걸린 sync 이벤트 주입 (IR 리셋 흉내)
        for sc in sync_counters:
            if i * SPC <= sc < (i + 1) * SPC:
                _feed(acq, packet.encode_sync(seq=10000 + sj, device_id=device_id,
                                              source_id=0, edge=1, seq_sync=sj, sample_idx=sc))
                sj += 1

    rec.close()
    return rec.path, kept


def verify(path: str, device_id: int, kept, gap_at: int | None = None,
           ch_count: int = CH, sync_counters: list[int] | None = None):
    """HDF5 를 ground truth 와 대조. (검사명, ok, 상세) 리스트."""
    sync_counters = sync_counters or []
    exp_samples, exp_counters = expected_stream(device_id, kept, SPC, ch_count, SR_HZ)
    fname = path.split("/")[-1]
    checks = []
    with h5py.File(path, "r") as h5:
        samp = h5["samples"][:]
        cnt = h5["sample_counter"][:]
        ts = h5["timestamps"][:]
        checks.append((f"파일명 = Device_{device_id:03d}_*",
                       fname.startswith(f"Device_{device_id:03d}_"), fname))
        checks.append((f"샘플 무손실 ({ch_count}ch, 기록==생성)",
                       samp.shape == exp_samples.shape and np.array_equal(samp, exp_samples),
                       f"{samp.shape} vs {exp_samples.shape}"))
        checks.append(("샘플 카운터 일치", np.array_equal(cnt, exp_counters), f"n={len(cnt)}"))
        checks.append(("타임스탬프 단조 증가",
                       ts.size > 1 and bool(np.all(np.diff(ts) > 0)), f"n={ts.size}"))
        checks.append(("메타데이터 (device_id/sr/ch)",
                       int(h5.attrs["device_id"]) == device_id and
                       int(h5.attrs["sr_hz"]) == SR_HZ and
                       int(h5.attrs["channel_count"]) == ch_count, "attrs"))
        if gap_at is not None:
            has = "event_markers" in h5 and any(
                int(r["code"]) == DISC_CODE and int(r["value"]) == SPC
                for r in h5["event_markers"][:])
            checks.append((f"갭@block{gap_at} → discontinuity 마커(missing={SPC})", has, "event_markers"))
        if sync_counters:
            n = len(h5["sync_events"]) if "sync_events" in h5 else 0
            checks.append((f"동기 이벤트 {len(sync_counters)}개 기록", n == len(sync_counters),
                           f"recorded={n}"))
    return checks


def run_backpressure(out_dir: str, n_blocks: int = 40, lossy_cap: int = 4):
    """느린(lossy) 소비자가 뷰를 버리는 동안 기록이 무손실인지.
    반환 (기록 샘플 수, 기대 샘플 수, lossy 소비자가 버린 수)."""
    acq = AcquisitionManager(SimulatedTransport(device_id=1))
    acq.start()
    rec = DeviceRecorder(out_dir, 1)
    lossy = RingBuffer(capacity=lossy_cap, drop_oldest=True)   # 시각화 흉내(비우지 않음)

    def on_block(blk):
        if rec.channel_count is None:
            rec.open(blk.channel_count, blk.sr_hz, blk.order)
        tb = acq.timebase(blk.device_id)
        counters = range(blk.first_counter, blk.first_counter + blk.n_samples)
        rec.write_block(blk, list(counters), tb.sample_times(blk.first_counter, blk.n_samples))

    acq.bus.subscribe(on_block)          # LOSSLESS 소비자 (기록)
    acq.bus.subscribe(lossy.push)        # LOSSY 소비자 (자기 뷰만 손실)

    for i in range(n_blocks):
        block = synth_block(1, i * SPC, SPC, CH, SR_HZ)
        _feed(acq, packet.encode_data(i, 1, block, i * SPC, SR_HZ, order=list(range(CH))))

    rec.close()
    with h5py.File(rec.path, "r") as h5:
        recorded = h5["samples"].shape[1]
    return recorded, n_blocks * SPC, lossy.dropped
