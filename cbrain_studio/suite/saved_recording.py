"""Read-only, bounded-memory access to completed CBRAIN recordings."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import h5py
import numpy as np

CHUNK = 65536


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('파일 읽기를 취소했습니다.')


def fingerprint(path):
    s = Path(path).stat()
    return s.st_size, s.st_mtime_ns


def plain(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


@dataclass
class Recording:
    path: Path
    stamp: tuple
    attrs: dict
    channels: int
    count: int
    sr: int
    order: list[int]
    first: int
    last: int
    missing: int
    gap_count: int
    scale: float
    units: str
    enc: int
    midpoint: float
    led_reported: bool
    warnings: list[str]

    @property
    def duration(self):
        return (self.last - self.first + 1) / self.sr if self.count else 0.

    @property
    def device(self):
        value = self.attrs.get('device_id')
        return f'CBRAIN_{value}' if value is not None else '기기 번호 미기록'


def inspect(path, cancel=None):
    path = Path(path).expanduser().resolve()
    stamp = fingerprint(path)
    warnings = []
    with h5py.File(path, 'r') as f:
        if 'samples' not in f or 'sample_counter' not in f:
            raise ValueError('CBRAIN 기록 파일이 아닙니다. samples와 sample_counter가 필요합니다.')
        samples, counters = f['samples'], f['sample_counter']
        if samples.ndim != 2 or not 1 <= samples.shape[0] <= 16 or samples.dtype.kind not in 'iu' or samples.dtype.itemsize != 2:
            raise ValueError('지원하는 샘플 형식은 1–16채널 × 샘플 수의 16비트 정수입니다.')
        channels, count = samples.shape
        if counters.ndim != 1 or counters.shape[0] != count or counters.dtype.kind not in 'iu':
            raise ValueError('샘플과 카운터 배열이 일치하지 않습니다. 저장 완료 여부를 확인하세요.')
        for name in ('timestamps', 'led_state'):
            if name in f and (f[name].ndim != 1 or f[name].shape[0] != count):
                raise ValueError(f'{name} 배열 길이가 다릅니다. 저장 완료 여부를 확인하세요.')
        attrs = {k: plain(v) for k, v in f.attrs.items()}
        sr = int(attrs.get('sr_hz', 0))
        if not 1 <= sr <= 1000000:
            raise ValueError('샘플레이트 정보가 없거나 올바르지 않습니다.')
        if 'n_samples' in attrs and int(attrs['n_samples']) != count:
            raise ValueError('종료 시 기록된 샘플 수와 파일 내용이 다릅니다.')
        if 'n_samples' not in attrs:
            warnings.append('정상 종료 정보 미기록')
        if 'channel_count' in attrs and int(attrs['channel_count']) != channels:
            raise ValueError('기록된 채널 수와 샘플 배열이 다릅니다.')
        order = [int(x) for x in attrs.get('channel_order', list(range(channels)))]
        if len(order) != channels or len(set(order)) != channels or any(x < 0 or x > 15 for x in order):
            raise ValueError('채널 순서 정보가 올바르지 않습니다.')
        enc = int(attrs.get('enc', 0))
        if enc not in (0, 1):
            raise ValueError('지원하지 않는 ADC 인코딩입니다.')
        midpoint = float(attrs.get('adc_midscale', 32767))
        if enc == 1 and (not math.isfinite(midpoint) or not 0 <= midpoint <= 65535):
            raise ValueError('ADC 중앙값이 올바르지 않습니다.')
        scale = attrs.get('uv_per_lsb')
        if scale is None and 'adc_fullscale_mv' in attrs:
            scale = float(attrs['adc_fullscale_mv']) * 1000 / 32768
            warnings.append('전압 환산값은 파일의 ADC 범위에서 계산')
        units = 'µV'
        if scale is None:
            scale, units = 1., 'ADC'
            warnings.append('전압 환산 정보 없음 · ADC 값으로 표시')
        scale = float(scale)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError('전압 환산값이 올바르지 않습니다.')
        if 'sample_source' not in attrs:
            warnings.append('센서 출처 미기록')
        elif attrs['sample_source'] != 'RHD2216':
            warnings.append('신호 출처: ' + str(attrs['sample_source']))
        first = last = missing = gap_count = 0
        previous = None
        for a in range(0, count, CHUNK):
            check_cancel(cancel)
            c = counters[a:a + CHUNK]
            if np.any(c < 0):
                raise ValueError('음수 샘플 카운터가 있습니다.')
            c = c.astype(np.uint64)
            if np.any(c[1:] <= c[:-1]) or (previous is not None and int(c[0]) <= previous):
                raise ValueError('카운터 중복 또는 역행이 있어 시간축을 구성할 수 없습니다.')
            if previous is None:
                first = int(c[0])
            else:
                step = int(c[0]) - previous
                missing += step - 1
                gap_count += int(step > 1)
            dif = c[1:] - c[:-1]
            missing += int(np.sum(dif - 1, dtype=np.uint64))
            gap_count += int(np.count_nonzero(dif > 1))
            previous = last = int(c[-1])
        if 'timestamps' not in f:
            warnings.append('호스트 시각 미기록')
        led_reported = bool(attrs.get('led_reported', False)) and 'led_state' in f
        if led_reported and (f['led_state'].dtype.kind not in 'iu' or f['led_state'].dtype.itemsize != 1):
            raise ValueError('LED 상태 배열의 형식이 올바르지 않습니다.')
    if fingerprint(path) != stamp:
        raise ValueError('읽는 동안 파일이 변경됐습니다. 녹화를 종료한 뒤 다시 여세요.')
    return Recording(path, stamp, attrs, channels, count, sr, order, first, last,
                     missing, gap_count, scale, units, enc,
                     midpoint, led_reported, warnings)


@dataclass
class ViewData:
    start: float
    span: float
    rows: list[int]
    x: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    line: bool
    breaks: np.ndarray
    led: np.ndarray | None
    sample_count: int


def lower_bound(dataset, target, count):
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        if int(dataset[mid]) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def voltage(raw, recording):
    if recording.enc == 1:
        # The legacy bit pattern may be stored in a signed i16 HDF5 dataset.
        raw = raw.astype(np.uint16).astype(np.float64) - recording.midpoint
    else:
        raw = raw.astype(np.int16).astype(np.float64)
    return raw * recording.scale


def read_view(recording, start, span, rows, pixels=1400, cancel=None):
    """Time-binned extrema retain short peaks; empty time bins stay NaN."""
    r = recording
    rows = sorted(set(int(row) for row in rows))
    if any(row < 0 or row >= r.channels for row in rows):
        raise ValueError('채널 선택이 올바르지 않습니다.')
    if not math.isfinite(start) or not math.isfinite(span) or start < 0 or span <= 0:
        raise ValueError('표시 시간 범위가 올바르지 않습니다.')
    if fingerprint(r.path) != r.stamp:
        raise ValueError('파일이 변경됐습니다. 녹화를 종료한 뒤 다시 여세요.')
    pixels = max(2, min(2400, int(pixels)))
    with h5py.File(r.path, 'r') as f:
        counters = f['sample_counter']
        a = lower_bound(counters, r.first + math.ceil(start * r.sr), r.count)
        b = lower_bound(counters, r.first + math.ceil((start + span) * r.sr), r.count)
        n = b - a
        if n <= pixels * 2:
            check_cancel(cancel)
            c = counters[a:b].astype(np.uint64)
            x = (c - r.first).astype(float) / r.sr
            values = voltage(f['samples'][:, a:b][rows], r)
            breaks = np.r_[True, (c[1:] - c[:-1]) != 1] if n else np.empty(0, bool)
            led = f['led_state'][a:b].astype(np.uint8) if r.led_reported else None
            result = ViewData(start, span, rows, x, values, values, True, breaks, led, n)
        else:
            lo = np.full((len(rows), pixels), np.inf)
            hi = np.full((len(rows), pixels), -np.inf)
            led = np.zeros(pixels, np.uint8) if r.led_reported else None
            for offset in range(a, b, CHUNK):
                check_cancel(cancel)
                end = min(offset + CHUNK, b)
                c = counters[offset:end].astype(np.uint64)
                x = (c - r.first).astype(float) / r.sr
                bins = np.clip(((x - start) * pixels / span).astype(int), 0, pixels - 1)
                values = voltage(f['samples'][:, offset:end][rows], r)
                for row, v in enumerate(values):
                    np.minimum.at(lo[row], bins, v)
                    np.maximum.at(hi[row], bins, v)
                if led is not None:
                    np.bitwise_or.at(led, bins, f['led_state'][offset:end])
            lo[~np.isfinite(lo)] = np.nan
            hi[~np.isfinite(hi)] = np.nan
            x = start + (np.arange(pixels) + .5) * span / pixels
            result = ViewData(start, span, rows, x, lo, hi, False,
                              np.ones(pixels, bool), led, n)
    if fingerprint(r.path) != r.stamp:
        raise ValueError('읽는 동안 파일이 변경됐습니다. 녹화를 종료한 뒤 다시 여세요.')
    return result
