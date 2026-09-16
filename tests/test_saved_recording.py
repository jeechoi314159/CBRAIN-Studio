import hashlib
import threading

import h5py
import numpy as np
import pytest

from cbrain_studio.suite import saved_recording as reader


def make_file(path, samples=None, counters=None, **attrs):
    if samples is None:
        samples = np.arange(400, dtype=np.int16).reshape(4, 100)
    n = samples.shape[1]
    with h5py.File(path, 'w') as f:
        f['samples'] = samples
        f['sample_counter'] = np.arange(n, dtype=np.uint64) if counters is None else counters
        f['timestamps'] = np.arange(n) / 100.
        f['led_state'] = np.zeros(n, np.uint8)
        f.attrs.update(sr_hz=100, channel_count=samples.shape[0], n_samples=n,
                       device_id=2, dongle_name='CBRAIN_Bridge_2',
                       uv_per_lsb=.195, sample_source='RHD2216', led_reported=True)
        f.attrs.update(attrs)
    return path


def test_readonly_scale_physical_channels_and_precise_high_counter(tmp_path):
    raw = np.array([[-100, 0, 100], [-32768, 0, 32767]], np.int16)
    path = make_file(tmp_path/'data.h5', raw, np.arange(3, dtype=np.uint64) + 2**53 + 1,
                     channel_order=[7, 2], adc_midscale=32767)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    r = reader.inspect(path)
    v = reader.read_view(r, 0, r.duration, [1, 0])
    assert r.device == 'CBRAIN_2' and r.order == [7, 2] and r.missing == 0
    np.testing.assert_array_equal(v.lo, raw * .195)
    np.testing.assert_array_equal(v.x, [0, .01, .02])
    assert v.line and v.rows == [0, 1] and r.duration == .03
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_time_overview_keeps_isolated_extrema_and_led_pulses(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, 'CHUNK', 127)
    raw = np.zeros((2, 10000), np.int16)
    raw[0, 9987] = 32123
    raw[1, 4123] = -30210
    path = make_file(tmp_path/'peaks.h5', raw)
    with h5py.File(path, 'r+') as f:
        f['led_state'][2001] = 1
        f['led_state'][2002] = 2
    r = reader.inspect(path)
    v = reader.read_view(r, 0, r.duration, [0, 1], pixels=20)
    assert not v.line and v.lo.shape == (2, 20) and v.sample_count == 10000
    assert np.nanmax(v.hi[0]) == 32123 * .195
    assert np.nanmin(v.lo[1]) == -30210 * .195
    assert v.led[4] == 3


def test_gaps_are_elapsed_time_and_never_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, 'CHUNK', 3)
    raw = np.ones((1, 6), np.int16)
    path = make_file(tmp_path/'gap.h5', raw, np.array([100, 101, 102, 201, 202, 203], np.uint64))
    r = reader.inspect(path)
    assert r.missing == 98 and r.gap_count == 1 and r.duration == 1.04
    v = reader.read_view(r, 0, r.duration, [0])
    np.testing.assert_array_equal(v.breaks, [True, False, False, True, False, False])
    np.testing.assert_array_equal(v.x, [0, .01, .02, 1.01, 1.02, 1.03])
    assert reader.read_view(r, .1, .5, [0]).sample_count == 0
    # Enough data to use the overview; bins without counter samples are blank.
    path2 = make_file(tmp_path/'overview-gap.h5', np.ones((1, 2000), np.int16),
                     np.r_[np.arange(1000), np.arange(2000, 3000)].astype(np.uint64))
    r2 = reader.inspect(path2)
    v2 = reader.read_view(r2, 0, 30, [0], pixels=30)
    assert np.all(np.isnan(v2.lo[0, 10:20]))
    assert np.all(np.isfinite(v2.lo[0, :10]))


def test_legacy_unsigned_bits_and_metadata_fallback(tmp_path):
    raw = np.array([[0, 32767, 65535]], np.uint16).view(np.int16)
    path = make_file(tmp_path/'legacy.h5', raw, enc=1, adc_midscale=32767, adc_fullscale_mv=5.)
    with h5py.File(path, 'r+') as f:
        for k in ['uv_per_lsb', 'sample_source', 'n_samples']:
            del f.attrs[k]
    r = reader.inspect(path)
    np.testing.assert_array_equal(reader.read_view(r, 0, .03, [0]).lo,
                                  np.array([[-32767, 0, 32768]]) * (5000/32768))
    assert r.units == 'µV' and len(r.warnings) == 3
    with h5py.File(path, 'r+') as f:
        del f.attrs['adc_fullscale_mv']
    r = reader.inspect(path)
    assert r.units == 'ADC' and r.scale == 1


@pytest.mark.parametrize('n,channels', [(0, 4), (1, 1), (10, 16)])
def test_empty_single_and_sixteen_channel_files(tmp_path, n, channels):
    path = make_file(tmp_path/'valid.h5', np.zeros((channels, n), np.int16))
    r = reader.inspect(path)
    v = reader.read_view(r, 0, max(.1, r.duration), range(channels))
    assert v.lo.shape == (channels, n) and v.sample_count == n
    hidden = reader.read_view(r, 0, max(.1, r.duration), [])
    assert hidden.lo.shape == (0, n)


@pytest.mark.parametrize('attrs', [dict(sr_hz=0), dict(n_samples=99), dict(channel_count=3),
    dict(channel_order=[0,0,2,3]), dict(enc=2), dict(uv_per_lsb=float('nan')),
    dict(uv_per_lsb=-1), dict(enc=1, adc_midscale=float('nan'))])
def test_invalid_metadata_rejected(tmp_path, attrs):
    with pytest.raises(ValueError):
        reader.inspect(make_file(tmp_path/'invalid.h5', **attrs))


@pytest.mark.parametrize('counters', [[0, 1, 1], [2, 1, 3], [-1, 0, 1]])
def test_duplicate_reversed_and_negative_counters_rejected(tmp_path, counters):
    with pytest.raises(ValueError):
        reader.inspect(make_file(tmp_path/'bad-counter.h5', np.zeros((1, 3), np.int16),
                                 np.array(counters, np.int64)))


def test_lengths_shapes_and_foreign_hdf5_rejected(tmp_path):
    path = make_file(tmp_path/'bad-length.h5')
    with h5py.File(path, 'r+') as f:
        del f['led_state']
        f['led_state'] = np.zeros(99, np.uint8)
    with pytest.raises(ValueError, match='led_state'):
        reader.inspect(path)
    path2 = tmp_path/'foreign.h5'
    with h5py.File(path2, 'w') as f:
        f['other'] = [1, 2]
    with pytest.raises(ValueError, match='CBRAIN'):
        reader.inspect(path2)


def test_cancellation_and_changed_file_are_detected(tmp_path):
    path = make_file(tmp_path/'data.h5')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(reader.Cancelled):
        reader.inspect(path, cancel)
    r = reader.inspect(path)
    with pytest.raises(reader.Cancelled):
        reader.read_view(r, 0, 1, [0], cancel=cancel)
    with h5py.File(path, 'r+') as f:
        f.attrs['new'] = 'changed after index'
    with pytest.raises(ValueError, match='변경'):
        reader.read_view(r, 0, 1, [0])
