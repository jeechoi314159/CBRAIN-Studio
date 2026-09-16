"""
BandPowerGate — frequency-to-lit 폐루프의 판정 로직 (알고리즘, Qt 무관).

의도: 특정 주파수 대역의 뇌파가 나타나면 LED 로 표시한다. 매 순간이 아니라
**최근 `window` 구간을 FFT** 해 대역 파워를 구하고, **캘리브레이션에서 정한 threshold**
(baseline 대역 파워의 mean + k·SD) 를 넘으면 활성(LED ON). 상태가 윈도우 스케일로만
바뀌어 안정적이다.

계약:
  add_calib(win)  — baseline 구간에서 반복 호출해 대역 파워 통계 누적
  finalize()      — threshold 확정 (ready=True)
  active(win)     — 현재 윈도우가 대역 파워 임계를 넘는가(감시 채널 중 하나라도)
`win` 은 (ch, N) 배열이며 마지막 `window` 샘플만 사용한다.
"""
from __future__ import annotations

import numpy as np

from cbrain_studio.app.detection import band_power


class BandPowerGate:
    def __init__(self, sr_hz: int, f_lo: float, f_hi: float, window: int,
                 k: float = 3.0, channels: list[int] | None = None):
        self.sr_hz = sr_hz
        self.f_lo = f_lo
        self.f_hi = f_hi
        self.window = int(window)
        self.k = k
        self.channels = channels          # None = 전체
        self._n = 0
        self._sum: np.ndarray | None = None
        self._sumsq: np.ndarray | None = None
        self.threshold: np.ndarray | None = None

    def _band_powers(self, win: np.ndarray) -> np.ndarray:
        w = win[:, -self.window:]
        return np.array([band_power(w[c], self.sr_hz, self.f_lo, self.f_hi)
                         for c in range(w.shape[0])])

    def add_calib(self, win: np.ndarray) -> None:
        if win.shape[1] < self.window:
            return                         # 아직 한 윈도우도 안 참
        bp = self._band_powers(win)
        if self._sum is None:
            self._sum = np.zeros_like(bp)
            self._sumsq = np.zeros_like(bp)
        self._sum += bp
        self._sumsq += bp * bp
        self._n += 1

    def finalize(self) -> bool:
        if self._n == 0 or self._sum is None:
            return False
        mean = self._sum / self._n
        var = np.maximum(self._sumsq / self._n - mean ** 2, 0.0)
        self.threshold = mean + self.k * np.sqrt(var)
        return True

    @property
    def ready(self) -> bool:
        return self.threshold is not None

    @property
    def n_calib(self) -> int:
        return self._n

    def active(self, win: np.ndarray) -> bool:
        if self.threshold is None or win.shape[1] < self.window:
            return False
        bp = self._band_powers(win)
        chs = self.channels if self.channels is not None else range(len(bp))
        return any(0 <= c < len(bp) and bp[c] > self.threshold[c] for c in chs)
