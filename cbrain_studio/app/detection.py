"""
Event Detection Engine — 알고리즘 독립(plugin) 검출기.

architecture §6.4 / project.md: 온라인 검출, **알고리즘 교체 가능**, EventMarker 생성.
`Detector` 계약을 구현하면 어떤 알고리즘(threshold, FFT/band-power, template…)이든
파이프라인에 끼울 수 있다. 아래 `ThresholdDetector` 가 첫 구현.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from cbrain_studio.core.types import EventMarker, SampleBlock

EVT_THRESHOLD = 4      # 이벤트 코드 (packet_protocol §6.5 EVENT)
EVT_BANDPOWER = 5      # 대역 파워 이벤트


def band_power(x: np.ndarray, sr_hz: int, f_lo: float, f_hi: float) -> float:
    """1D 신호의 [f_lo, f_hi] Hz 대역 파워 (Hanning 창 + rfft, DC 제거)."""
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    n = x.size
    mag = np.abs(np.fft.rfft(x * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / sr_hz)
    band = (freqs >= f_lo) & (freqs < f_hi)
    return float(np.sum(mag[band] ** 2))


class Detector(ABC):
    """온라인 검출기 계약. 블록마다 EventMarker 리스트 반환. 상태는 구현이 유지."""

    @abstractmethod
    def process(self, block: SampleBlock) -> list[EventMarker]:
        ...


class ThresholdDetector(Detector):
    """
    value > threshold **상승 에지**에서 이벤트 (채널별). calibration threshold 사용.
    refractory(불응기, 샘플) 동안은 재발화 금지. 블록 경계를 넘어 상태를 유지한다.

    FFT/band-power 등 다른 알고리즘은 `Detector` 를 구현해 그대로 교체 가능.
    """

    def __init__(self, threshold: np.ndarray, refractory: int = 32,
                 channels: list[int] | None = None):
        self.threshold = np.asarray(threshold, dtype=np.float64)
        self.refractory = refractory
        self.channels = channels                # None = 전 채널
        self._prev_above: np.ndarray | None = None
        self._last_fire: np.ndarray | None = None

    def process(self, block: SampleBlock) -> list[EventMarker]:
        ch = block.channel_count
        if self._prev_above is None:
            self._prev_above = np.zeros(ch, dtype=bool)
            self._last_fire = np.full(ch, -(1 << 30), dtype=np.int64)

        chans = self.channels if self.channels is not None else range(ch)
        markers: list[EventMarker] = []
        for c in chans:
            th = self.threshold[c]
            above = block.channels[c].astype(np.float64) > th
            prev = np.concatenate(([self._prev_above[c]], above[:-1]))
            rising = np.nonzero(above & ~prev)[0]                 # 상승 에지 인덱스
            for k in rising:
                counter = block.first_counter + int(k)
                if counter - self._last_fire[c] >= self.refractory:
                    markers.append(EventMarker(block.device_id, counter, block.decode_time,
                                               EVT_THRESHOLD, c, "threshold"))
                    self._last_fire[c] = counter
            self._prev_above[c] = bool(above[-1])
        return markers


class BandPowerDetector(Detector):
    """
    대역 파워가 threshold 를 넘는 채널에서 이벤트 (윈도우 단위). `ThresholdDetector`
    와 **동일한 Detector 계약** 을 구현하므로 파이프라인 코드를 안 바꾸고 교체된다
    (알고리즘 독립성 증명). 블록 경계를 넘어 윈도우를 누적한다.
    """

    def __init__(self, f_lo: float, f_hi: float, sr_hz: int, window: int,
                 threshold: float, refractory: int | None = None,
                 channels: list[int] | None = None):
        self.f_lo, self.f_hi, self.sr = f_lo, f_hi, sr_hz
        self.window = window
        self.threshold = threshold
        self.refractory = refractory if refractory is not None else window
        self.channels = channels
        self._buf: np.ndarray | None = None
        self._fill = 0
        self._last_fire: np.ndarray | None = None

    def process(self, block: SampleBlock) -> list[EventMarker]:
        ch = block.channel_count
        if self._buf is None:
            self._buf = np.zeros((ch, self.window))
            self._last_fire = np.full(ch, -(1 << 30), dtype=np.int64)

        markers: list[EventMarker] = []
        x = block.channels.astype(np.float64)
        i, n = 0, block.n_samples
        while i < n:
            take = min(self.window - self._fill, n - i)
            self._buf[:, self._fill:self._fill + take] = x[:, i:i + take]
            self._fill += take
            i += take
            if self._fill == self.window:
                end = block.first_counter + i
                chans = self.channels if self.channels is not None else range(ch)
                for c in chans:
                    if band_power(self._buf[c], self.sr, self.f_lo, self.f_hi) > self.threshold:
                        if end - self._last_fire[c] >= self.refractory:
                            markers.append(EventMarker(block.device_id, end, block.decode_time,
                                                       EVT_BANDPOWER, c, "bandpower"))
                            self._last_fire[c] = end
                self._fill = 0
        return markers
