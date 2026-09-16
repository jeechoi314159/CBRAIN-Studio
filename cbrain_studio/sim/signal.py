"""
결정적 합성 신경 신호 — 시뮬레이션 검증용 ground truth.

`(device_id, first_counter)` 만으로 완전히 재현되므로, 송신(프레임 인코딩)과
검증(기대값)이 **같은 함수**를 써서 바이트 단위로 대조할 수 있다.

신호 구성 (검증 목적별):
  - 채널별 서로 다른 주파수 sine  → 채널 매핑/대역 확인
  - 디바이스·채널별 DC 오프셋       → 디바이스 구분(멀티 디바이스)
  - ch0 주기적 임펄스               → event detection 용
"""
from __future__ import annotations

import numpy as np

IMPULSE_PERIOD = 512     # samples; ch0 임펄스 주기 (event detection ground truth)
IMPULSE_AMP = 20000      # sine peak(8000) 위로 확실히 → threshold 검출 가능
SINE_AMP = 8000.0


def synth_block(device_id: int, first_counter: int, n_samples: int,
                ch_count: int, sr_hz: int, impulses: bool = True) -> np.ndarray:
    """(ch_count × n_samples) int16 블록을 결정적으로 생성한다.
    impulses=False 면 ch0 임펄스 없는 깨끗한 baseline (calibration 용)."""
    idx = first_counter + np.arange(n_samples, dtype=np.int64)
    out = np.empty((ch_count, n_samples), dtype=np.int16)
    for c in range(ch_count):
        f = 5 + 3 * c                                  # 5, 8, 11, … Hz
        sig = SINE_AMP * np.sin(2 * np.pi * f * idx / sr_hz)
        sig = sig + (200 * device_id + 50 * c)         # device/channel 구분 오프셋
        if impulses and c == 0:
            sig = sig + np.where(idx % IMPULSE_PERIOD == 0, IMPULSE_AMP, 0)
        out[c] = np.clip(sig, -32768, 32767).astype(np.int16)
    return out


def impulse_counters(n_total: int) -> list[int]:
    """[0, n_total) 구간의 ch0 임펄스 위치(카운터) ground truth."""
    return list(range(0, n_total, IMPULSE_PERIOD))


def expected_stream(device_id: int, block_indices, spc: int,
                    ch_count: int, sr_hz: int):
    """주어진 (기록된) 블록 인덱스들에 대한 (samples[ch,N], counters[N])."""
    blocks, counters = [], []
    for i in block_indices:
        c0 = i * spc
        blocks.append(synth_block(device_id, c0, spc, ch_count, sr_hz))
        counters.extend(range(c0, c0 + spc))
    samples = np.concatenate(blocks, axis=1) if blocks else np.empty((ch_count, 0), np.int16)
    return samples, np.asarray(counters, dtype=np.uint64)
