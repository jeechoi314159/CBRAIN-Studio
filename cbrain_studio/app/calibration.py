"""
Calibration Manager — baseline 기록에서 채널별 통계를 추정하고 threshold 를 추천한다.

project.md: mean·SD 추정, threshold 추천.  **Threshold = Mean + k·SD** (k 사용자 설정).
채널 수는 블록에서 읽어 적응한다(하드코딩 없음). 스트리밍 누적(sum/sumsq)이라
임의 길이 baseline 을 한 번에 담지 않아도 된다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cbrain_studio.core.types import SampleBlock


@dataclass
class CalibrationResult:
    device_id: int
    channel_count: int
    mean: np.ndarray        # (ch,)
    sd: np.ndarray          # (ch,)  모집단 표준편차
    threshold: np.ndarray   # (ch,)  = mean + k·sd
    n_samples: int
    k: float


class CalibrationManager:
    """baseline SampleBlock 을 누적해 채널별 mean/SD/threshold 를 계산."""

    def __init__(self, k: float = 3.0):
        self.k = k
        self._n = 0
        self._sum: np.ndarray | None = None    # (ch,) float64
        self._sumsq: np.ndarray | None = None
        self._ch: int | None = None
        self._device_id: int | None = None

    def add_block(self, block: SampleBlock) -> None:
        x = block.channels.astype(np.float64)          # (ch, n)
        if self._sum is None:
            self._ch = block.channel_count
            self._device_id = block.device_id
            self._sum = np.zeros(self._ch)
            self._sumsq = np.zeros(self._ch)
        self._sum += x.sum(axis=1)
        self._sumsq += (x * x).sum(axis=1)
        self._n += block.n_samples

    def result(self) -> CalibrationResult:
        if self._n == 0:
            raise RuntimeError("baseline 이 비었습니다 (add_block 먼저)")
        mean = self._sum / self._n
        var = np.maximum(self._sumsq / self._n - mean ** 2, 0.0)
        sd = np.sqrt(var)
        return CalibrationResult(self._device_id, self._ch, mean, sd,
                                 mean + self.k * sd, self._n, self.k)
