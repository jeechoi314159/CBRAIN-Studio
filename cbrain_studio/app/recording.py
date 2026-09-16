"""
Recording Manager — 디바이스당 1파일, 무손실 기록.

각 디바이스는 자기 파일에 기록한다(교차 디바이스 병합 없음). 원샘플·카운터·
절대시각·이벤트마커·동기이벤트·메타데이터를 보존한다. 획득한 데이터는 절대
조용히 버리지 않으며, 카운터 갭은 discontinuity 마커로 남긴다.

HDF5(권장). h5py 미설치 시 예외로 알린다(무손실 기록은 조용히 폴백하지 않음).
파일명: Device_<id>_<YYYY-MM-DD>_<HHMMSS>.h5
"""
from __future__ import annotations

import datetime as _dt
import os

import numpy as np

from cbrain_studio.core.packet import led_state
from cbrain_studio.core.types import (
    ADC_FULLSCALE_MV,
    ADC_MIDSCALE,
    EventMarker,
    SampleBlock,
    SyncEvent,
)

try:
    import h5py
    _HAS_H5PY = True
except Exception:  # noqa: BLE001
    _HAS_H5PY = False


class DeviceRecorder:
    """단일 디바이스 → 단일 HDF5 파일. 채널 수는 첫 블록에서 결정(가변)."""

    def __init__(self, out_dir: str, device_id: int, metadata: dict | None = None):
        if not _HAS_H5PY:
            raise RuntimeError("h5py 필요: pip install h5py (무손실 기록은 폴백하지 않음)")
        self.out_dir = out_dir
        self.device_id = device_id
        self.metadata = metadata or {}
        self._h5 = None
        self._ds_samp = None
        self._ds_cnt = None
        self._ds_ts = None
        self._ds_led = None
        self._led_reported = False       # 기기가 실측 LED 를 보고했는가 (구형 fw=False)
        self.path: str | None = None
        self.n_written = 0
        self.channel_count: int | None = None
        self._markers: list[tuple] = []
        self._syncs: list[tuple] = []

    def open(self, channel_count: int, sr_hz: int, order: list[int]) -> str:
        os.makedirs(self.out_dir, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.path = os.path.join(self.out_dir, f"Device_{self.device_id:03d}_{ts}.h5")
        self.channel_count = channel_count
        h5 = h5py.File(self.path, "w")
        h5.attrs.update(dict(
            device_id=self.device_id, sr_hz=sr_hz, channel_count=channel_count,
            channel_order=np.array(order, dtype=np.int16),
            adc_midscale=ADC_MIDSCALE, adc_fullscale_mv=ADC_FULLSCALE_MV,
            software_version="0.1.0-dev", proto="CB v2", created=ts,
        ))
        for k, v in self.metadata.items():
            try:
                h5.attrs[k] = v
            except Exception:  # noqa: BLE001
                h5.attrs[k] = str(v)
        self._ds_samp = h5.create_dataset(
            "samples", shape=(channel_count, 0), maxshape=(channel_count, None),
            dtype="i2", chunks=(channel_count, 8192), compression="gzip", compression_opts=4)
        self._ds_cnt = h5.create_dataset(
            "sample_counter", shape=(0,), maxshape=(None,), dtype="u8",
            chunks=(8192,), compression="gzip", compression_opts=4)
        self._ds_ts = h5.create_dataset(
            "timestamps", shape=(0,), maxshape=(None,), dtype="f8",
            chunks=(8192,), compression="gzip", compression_opts=4)
        # 기기 실측 LED 상태(샘플당). bit0=LED0(UI LED1), bit1=LED1(UI LED2).
        # 블록 내 상수라 gzip 압축이 거의 공짜. 유효성은 파일 attr led_reported.
        self._ds_led = h5.create_dataset(
            "led_state", shape=(0,), maxshape=(None,), dtype="u1",
            chunks=(8192,), compression="gzip", compression_opts=4)
        self._ds_led.attrs["bits"] = "bit0=LED0 (Studio LED1), bit1=LED1 (Studio LED2)"
        self._h5 = h5
        return self.path

    def write_block(self, block: SampleBlock, counters, timestamps) -> None:
        n = block.n_samples
        if n == 0:
            return
        new = self.n_written + n
        self._ds_samp.resize(new, axis=1)
        self._ds_samp[:, self.n_written:new] = block.channels
        self._ds_cnt.resize(new, axis=0)
        self._ds_cnt[self.n_written:new] = np.asarray(counters, dtype=np.uint64)
        self._ds_ts.resize(new, axis=0)
        self._ds_ts[self.n_written:new] = np.asarray(timestamps, dtype=np.float64)
        # 실측 LED (블록 flags). 보고 안 하는 fw 는 0(led_reported=False 로 구분).
        ls = led_state(int(getattr(block, "flags", 0) or 0))
        led_byte = 0
        if ls is not None:
            self._led_reported = True
            led_byte = (1 if ls[0] else 0) | (2 if ls[1] else 0)
        self._ds_led.resize(new, axis=0)
        self._ds_led[self.n_written:new] = led_byte
        self.n_written = new

    def add_marker(self, m: EventMarker) -> None:
        self._markers.append((m.counter, m.host_time, m.code, m.value, m.label))

    def add_sync(self, s: SyncEvent) -> None:
        self._syncs.append((s.counter if s.counter is not None else -1,
                            s.host_time, s.source, s.edge, s.value))

    def add_discontinuity(self, counter: int, host_time: float, missing: int) -> None:
        # 갭은 마커로 명시 기록 (조용한 손실 금지)
        self._markers.append((counter, host_time, 0xFF, missing, "discontinuity"))

    def close(self) -> str | None:
        if self._h5 is None:
            return None
        if self._markers:
            self._write_table("event_markers", self._markers,
                              ["counter", "host_time", "code", "value", "label"])
        if self._syncs:
            self._write_table("sync_events", self._syncs,
                              ["counter", "host_time", "source", "edge", "value"])
        self._h5.attrs["n_samples"] = self.n_written
        self._h5.attrs["led_reported"] = bool(self._led_reported)
        self._h5.close()
        self._h5 = None
        return self.path

    def _write_table(self, name, rows, cols):
        dt = np.dtype([
            (c, "f8" if c in ("host_time",) else ("u8" if c == "counter" else "i8"))
            if c not in ("label", "source") else (c, h5py.string_dtype())
            for c in cols
        ])
        arr = np.empty(len(rows), dtype=dt)
        for i, r in enumerate(rows):
            for c, v in zip(cols, r):
                arr[c][i] = v
        self._h5.create_dataset(name, data=arr)
