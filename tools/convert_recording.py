#!/usr/bin/env python3
"""
CBRAIN 기록(HDF5) → 수치값 변환. ADC 원값을 µV 로 바꿔 CSV/NPZ 로 내보낸다.

- 샘플을 **µV** 로 변환(enc 인식: 2's-comp/offset-binary).
- 시간축: 카운터 기반 상대시간 `t_s` + 호스트 도착시각 `host_time`.
- 이벤트/discontinuity 마커도 별도 CSV 로.

사용:
  python tools/convert_recording.py recordings/Device_001_….h5
  python tools/convert_recording.py <file.h5> --format both --out ./export
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from cbrain_studio.core.types import UV_PER_LSB, raw_to_microvolts

try:
    import h5py
except Exception as e:  # noqa: BLE001
    raise SystemExit(f"h5py 필요: pip install h5py ({e})")


def convert(path: str, out_dir: str, fmt: str) -> list[str]:
    written: list[str] = []
    with h5py.File(path, "r") as f:
        raw = f["samples"][()]                       # (ch, N) int16
        counter = f["sample_counter"][()].astype(np.int64)
        host_time = f["timestamps"][()].astype(np.float64)
        a = dict(f.attrs)
        sr = int(a.get("sr_hz", 1024))
        enc = int(a.get("enc", 0))
        order = list(np.asarray(a.get("channel_order", np.arange(raw.shape[0]))).ravel())
        scale = float(a.get('uv_per_lsb', UV_PER_LSB))
        if not np.isfinite(scale) or scale <= 0:raise ValueError('Invalid recording voltage scale')
        uv = raw_to_microvolts(raw, enc) * (scale / UV_PER_LSB)
        n = raw.shape[1]
        t_s = (counter - (counter[0] if n else 0)) / float(sr)   # 카운터 기반 상대시간

        markers = f["event_markers"][()] if "event_markers" in f else None
        led = f["led_state"][()] if "led_state" in f else None
        led_reported = bool(a.get("led_reported", False))

    base = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(out_dir, exist_ok=True)

    labels = [f"ch{order[i] if i < len(order) else i}_uV" for i in range(uv.shape[0])]
    have_led = led_reported and led is not None
    if have_led:
        led1 = (led & 1).astype(np.int64)          # bit0 = LED0 → Studio LED1
        led2 = ((led >> 1) & 1).astype(np.int64)   # bit1 = LED1 → Studio LED2
    print(f"[in ] {path}: {uv.shape[0]}ch × {n} samples @ {sr}Hz, enc={enc}, "
          f"{scale:.5f} µV/LSB")

    stack = [t_s, host_time, counter, uv.T]
    header = "t_s,host_time,sample_counter," + ",".join(labels)
    fmts = ["%.6f", "%.6f", "%d"] + ["%.3f"] * uv.shape[0]
    if have_led:
        stack += [led1, led2]
        header += ",led1,led2"
        fmts += ["%d", "%d"]

    if fmt in ("csv", "both"):
        cols = np.column_stack(stack)             # (N, 3+ch[+2])
        csv_path = os.path.join(out_dir, base + ".csv")
        np.savetxt(csv_path, cols, delimiter=",", header=header, comments="", fmt=fmts)
        written.append(csv_path)
        if markers is not None:
            mpath = os.path.join(out_dir, base + "_markers.csv")
            _write_markers_csv(mpath, markers, sr, counter[0] if n else 0)
            written.append(mpath)

    if fmt in ("npz", "both"):
        npz_path = os.path.join(out_dir, base + ".npz")
        kw = dict(t_s=t_s, host_time=host_time, sample_counter=counter, uv=uv,
                  channels=np.asarray(order), sr_hz=sr)
        if have_led:
            kw.update(led1=led1, led2=led2)
        np.savez_compressed(npz_path, **kw)
        written.append(npz_path)

    for w in written:
        print(f"[out] {w}")
    return written


def _write_markers_csv(path: str, markers: np.ndarray, sr: int, c0: int) -> None:
    names = list(markers.dtype.names)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("t_s," + ",".join(names) + "\n")
        for row in markers:
            t = (int(row["counter"]) - int(c0)) / float(sr) if "counter" in names else 0.0
            vals = []
            for nm in names:
                v = row[nm]
                vals.append(v.decode() if isinstance(v, bytes) else str(v))
            fp.write(f"{t:.6f}," + ",".join(vals) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="CBRAIN 기록 → 수치값(CSV/NPZ)")
    ap.add_argument("file", help="Device_*.h5 경로")
    ap.add_argument("--out", default=None, help="출력 디렉터리(기본: 입력 파일과 같은 곳)")
    ap.add_argument("--format", choices=["csv", "npz", "both"], default="csv")
    args = ap.parse_args()
    out_dir = args.out or os.path.dirname(os.path.abspath(args.file))
    convert(args.file, out_dir, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
