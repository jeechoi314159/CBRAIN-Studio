"""
타임베이스 — 디바이스 32-bit 샘플 카운터 → 절대시각, 그리고 갭(불연속) 감지.

무손실 불변식의 핵심: 카운터 연속성을 검사해, 끊김이 있으면 조용히 버리지 않고
Discontinuity 를 반환한다(상위가 마커로 기록). 카운터는 32-bit 래핑을 보정한다.

절대시각 = session_epoch + (unwrapped_counter - start_counter) / sr_hz
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Discontinuity:
    device_id: int
    expected_counter: int
    got_counter: int
    missing_samples: int


class TimeBase:
    def __init__(self, sr_hz: int, session_epoch: float | None = None) -> None:
        self.sr_hz = sr_hz
        self.session_epoch = session_epoch if session_epoch is not None else time.time()
        self._start_counter: int | None = None
        self._unwrapped: int = 0
        self._last_counter: int | None = None
        self._prev_n: int = 0

    def _unwrap(self, counter: int) -> int:
        if self._last_counter is None:
            self._start_counter = counter
            self._unwrapped = 0
        else:
            self._unwrapped += (counter - self._last_counter) & 0xFFFFFFFF
        self._last_counter = counter
        return self._unwrapped

    def observe(self, first_counter: int, n_samples: int) -> Discontinuity | None:
        """블록 도착 시 호출. 연속이 아니면 Discontinuity 반환(마커 기록용)."""
        gap = None
        if self._last_counter is not None:
            expected = (self._last_counter + self._prev_n) & 0xFFFFFFFF
            if first_counter != expected:
                missing = (first_counter - expected) & 0xFFFFFFFF
                gap = Discontinuity(0, expected, first_counter, missing)
        self._prev_n = n_samples
        self._unwrap(first_counter)
        return gap

    def absolute_time(self, counter: int) -> float:
        """주어진(원시 32-bit) 카운터의 절대시각(epoch 초)."""
        if self._start_counter is None:
            return self.session_epoch
        # 현재 unwrap 상태 기준 근사(단조 스트림 가정)
        rel = (counter - self._start_counter) & 0xFFFFFFFF
        return self.session_epoch + rel / float(self.sr_hz)

    def sample_times(self, first_counter: int, n_samples: int):
        base = self.absolute_time(first_counter)
        dt = 1.0 / float(self.sr_hz)
        return [base + k * dt for k in range(n_samples)]
