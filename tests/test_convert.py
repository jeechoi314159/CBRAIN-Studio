"""기록 → 수치값(µV) 변환 검증."""
from __future__ import annotations

import importlib.util
import os

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from cbrain_studio.app.recording import DeviceRecorder  # noqa: E402
from cbrain_studio.core.types import UV_PER_LSB, SampleBlock  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_converter():
    path = os.path.join(_ROOT, "tools", "convert_recording.py")
    spec = importlib.util.spec_from_file_location("convrec", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_convert_to_csv_microvolts(tmp_path):
    rec = DeviceRecorder(str(tmp_path), 1, metadata={"enc": 0, "experiment": "cnv"})
    rec.open(2, 1024, [0, 1])
    ch = np.array([[1000, 2000, -1000], [0, -2000, 500]], dtype=np.int16)   # (2,3)
    blk = SampleBlock(1, 0, 1024, channels=ch, order=[0, 1])
    rec.write_block(blk, [0, 1, 2], [0.0, 0.001, 0.002])
    path = rec.close()

    conv = _load_converter()
    outs = conv.convert(path, str(tmp_path / "export"), "csv")
    csv = next(p for p in outs if p.endswith(".csv") and "markers" not in p)

    rows = np.genfromtxt(csv, delimiter=",", names=True)
    # 헤더: t_s, host_time, sample_counter, ch0_uV, ch1_uV
    assert list(rows["sample_counter"]) == [0, 1, 2]
    assert np.allclose(rows["t_s"], [0.0, 1 / 1024, 2 / 1024], atol=1e-5)   # CSV %.6f 반올림
    assert np.allclose(rows["ch0_uV"], np.array([1000, 2000, -1000]) * UV_PER_LSB, atol=1e-2)
    assert np.allclose(rows["ch1_uV"], np.array([0, -2000, 500]) * UV_PER_LSB, atol=1e-2)


def test_led_saved_and_converted(tmp_path):
    import cbrain_studio.core.packet as pk
    rec = DeviceRecorder(str(tmp_path), 3, metadata={"enc": 0})
    rec.open(1, 1024, [0])
    ch = np.array([[10, 20]], dtype=np.int16)
    # block1: LED1 on (bit0); block2: both off — both blocks report (LED_VALID)
    rec.write_block(SampleBlock(3, 0, 1024, channels=ch, order=[0],
                                flags=pk.FLAG_LED_VALID | pk.FLAG_LED0), [0, 1], [0.0, 0.001])
    rec.write_block(SampleBlock(3, 2, 1024, channels=ch, order=[0],
                                flags=pk.FLAG_LED_VALID), [2, 3], [0.002, 0.003])
    path = rec.close()

    with h5py.File(path, "r") as f:
        assert bool(f.attrs["led_reported"]) is True
        assert list(f["led_state"][()]) == [1, 1, 0, 0]          # LED1 on for block1 only

    conv = _load_converter()
    outs = conv.convert(path, str(tmp_path / "export"), "both")
    rows = np.genfromtxt(next(p for p in outs if p.endswith(".csv") and "markers" not in p),
                         delimiter=",", names=True)
    assert list(rows["led1"]) == [1, 1, 0, 0] and list(rows["led2"]) == [0, 0, 0, 0]
    npz = np.load(next(p for p in outs if p.endswith(".npz")))
    assert list(npz["led1"]) == [1, 1, 0, 0]


def test_no_led_reported_when_flags_zero(tmp_path):
    rec = DeviceRecorder(str(tmp_path), 4, metadata={"enc": 0})
    rec.open(1, 1024, [0])
    rec.write_block(SampleBlock(4, 0, 1024, channels=np.array([[1, 2]], np.int16), order=[0]),
                    [0, 1], [0.0, 0.001])                         # flags=0 (old firmware)
    path = rec.close()
    with h5py.File(path, "r") as f:
        assert bool(f.attrs["led_reported"]) is False
        assert list(f["led_state"][()]) == [0, 0]


def test_convert_npz(tmp_path):
    rec = DeviceRecorder(str(tmp_path), 2, metadata={"enc": 0})
    rec.open(1, 1024, [0])
    rec.write_block(SampleBlock(2, 0, 1024, channels=np.array([[100, 200]], np.int16), order=[0]),
                    [0, 1], [0.0, 0.001])
    path = rec.close()

    conv = _load_converter()
    outs = conv.convert(path, str(tmp_path / "export"), "npz")
    npz = np.load(next(p for p in outs if p.endswith(".npz")))
    assert np.allclose(npz["uv"][0], np.array([100, 200]) * UV_PER_LSB, atol=1e-2)
    assert int(npz["sr_hz"]) == 1024
