import numpy as np
import pytest
import h5py
from test_suite import unit, block, ready
from cbrain_studio.core import packet
from cbrain_studio.suite.runtime import Experiment
from cbrain_studio.suite.source import SOURCE_VALID, SENSOR_FAULT, legacy_test_pattern


def saw(counter=0, n=16, order=None):
    b=block(counter=counter,n=n);b.flags=0x8000
    b.order=order or [0,1,2,3]
    samples=np.arange(n,dtype=np.int64)+counter
    b.channels=np.array([(((samples*(7+3*c))&1023)-512)*16 for c in range(4)],dtype=np.int16)
    return b


@pytest.mark.parametrize('counter',[0,1021,7654321,0xfffffff8])
def test_exact_old_firmware_wave_is_rejected_before_display_or_listeners(counter,tmp_path):
    u=unit();seen=[];u.listeners.append(seen.append)
    u.block(saw(counter,order=[7,3,1,0]))
    assert u.source=='synthetic' and '시험 신호' in u.status
    assert u.samples==0 and u.last_block is None and not u.ready and not seen
    assert u.buffer.snapshot(10,100) is None
    with pytest.raises(RuntimeError):Experiment(tmp_path,'invalid',[u])


def test_periodic_or_near_matching_signal_is_not_called_synthetic():
    b=saw();b.channels[0,8]+=1
    assert not legacy_test_pattern(b)
    b=block();b.channels[:]=np.tile([0,400,-400,0],8)
    assert not legacy_test_pattern(b)


def test_unreported_source_is_never_ready_or_recordable(tmp_path):
    u=unit();seen=[];u.listeners.append(seen.append)
    for counter in [0,32]:
        b=block(counter=counter);b.flags=0;u.block(b)
    u.first_time=u.last_time-5
    assert not u.ready and not u.error and not seen and '센서 확인 불가' in u.status
    with pytest.raises(RuntimeError):Experiment(tmp_path,'old',[u])


@pytest.mark.parametrize('reason',[1,2,3,4,5])
def test_explicit_fault_frame_reaches_unit_through_decoder(reason):
    u=unit()
    wire=packet.encode_data(seq=0,device_id=1,first_counter=0,sr_hz=1024,channels=np.zeros((4,16),dtype=np.int16),
                            flags=SOURCE_VALID|SENSOR_FAULT|(reason<<4))
    # Real dongles split CB frames across USB envelopes. Decode fragmented input.
    for offset in range(0,len(wire),7):u.feed(wire[offset:offset+7])
    assert u.source=='sensor_fault' and '센서 오류' in u.status
    assert u.samples==0 and u.last_block is None


@pytest.mark.parametrize('mode',['fault','synthetic','unreported'])
def test_sensor_failure_during_record_stops_writes_and_marks_incomplete(tmp_path,mode):
    u=unit();ready(u);e=Experiment(tmp_path,'sensor-fail',[u])
    b=block(counter=64)
    if mode=='fault':b.flags=SOURCE_VALID|SENSOR_FAULT|(2<<4)
    elif mode=='synthetic':b=saw(64,n=32)
    else:b.flags=0
    u.block(b);summary=e.stop()[0]
    assert not u.ready and summary['error'] and not summary['complete'] and summary['samples']==0


def test_rhd_scale_preserved_in_recording_and_export(tmp_path):
    from tools.convert_recording import convert
    u=unit();ready(u);assert u.ready and u.uv_per_lsb==.195
    e=Experiment(tmp_path,'sensor',[u]);u.block(block(counter=64));s=e.stop()[0]
    assert s['complete']
    with h5py.File(s['file']) as f:
        assert f.attrs['sample_source']=='RHD2216' and f.attrs['uv_per_lsb']==.195
    paths=convert(s['file'],str(tmp_path/'export'),'npz')
    assert np.allclose(np.load(paths[0])['uv'],42*.195)
