import binascii
import threading
import time
from types import SimpleNamespace
import numpy as np
import pytest
import h5py
from cbrain_studio.suite.usb import Dongle,Candidate,allocate,EnvelopeDecoder,USBBridge,Lease,UP
from cbrain_studio.suite.runtime import Unit,Experiment,Registry
from cbrain_studio.core.types import SampleBlock
from cbrain_studio.suite.source import SOURCE_VALID


def candidate(device,serial='A',address=None,token=1):
    return Candidate(Dongle('/fake/'+serial,serial),device,token,address or bytes([device])*7,-45,time.monotonic())
def unit(device=1):
    bridge=SimpleNamespace(dongle=Dongle('/fake','TEST'+str(device)),firmware='1.0.0')
    return Unit(bridge,candidate(device))
def block(device=1,counter=0,n=32):
    return SampleBlock(device,counter,1024,np.full((4,n),42,dtype=np.int16),[0,1,2,3],flags=SOURCE_VALID)
def ready(u):
    u.block(block(u.id,0));u.block(block(u.id,32));u.first_time=u.last_time-3

def test_matching_non_greedy_and_duplicate_identity():
    rows=[candidate(1,'A'),candidate(1,'B'),candidate(3,'A')]
    result=allocate(rows,[1,3]);assert result[1].dongle.serial=='B' and result[3].dongle.serial=='A'
    with pytest.raises(RuntimeError):allocate(rows,[1,3,7])
    with pytest.raises(RuntimeError):allocate([candidate(1,'A'),candidate(1,'B',b'abcdefg')],[1])

def test_wrong_id_never_reaches_display_or_writer():
    u=unit();u.block(block(3));assert u.error and u.samples==0 and u.last_block is None

def test_counter_reset_and_format_change():
    u=unit();ready(u);u.block(block(counter=0));assert '카운터' in u.error
    u=unit();ready(u);b=block(counter=64);b.sr_hz=512;u.block(b);assert '형식' in u.error

def test_wrap_and_gap():
    u=unit();u.block(block(counter=0xffffffe0));u.block(block(counter=0));assert u.gaps==0
    u.block(block(counter=64));assert u.gaps==32 and not u.error

def test_two_device_record_and_metadata_preservation(tmp_path):
    units=[unit(1),unit(3)]
    for u in units:ready(u)
    e=Experiment(tmp_path,'mouse study',units);e.event('stimulus')
    for u in units:
        for i in range(64,384,32):u.block(block(u.id,i))
    e.tick();summaries=e.stop()
    assert len(summaries)==2 and all(s['complete'] for s in summaries)
    for s in summaries:
        with h5py.File(s['file']) as f:
            assert f['samples'].shape==(4,320)
            assert f.attrs['device_id']==s['device_id']
            assert np.array_equal(f['sample_counter'][:],np.arange(64,384))
    import json
    doc=json.loads((e.folder/'session.json').read_text());assert doc['name']=='mouse study' and doc['closed'] and doc['events'][0]['label']=='stimulus'
    assert (e.folder/'integrity.csv').exists()
    e2=Experiment(tmp_path,'second',units);assert e2.folder!=e.folder;e2.stop()

def test_mismatch_during_record_is_visible_and_not_written(tmp_path):
    u=unit();ready(u);e=Experiment(tmp_path,'test',[u]);u.block(block(3,64));s=e.stop()[0]
    assert not s['complete'] and '불일치' in s['error'] and s['samples']==0

def test_writer_error_surfaces(tmp_path,monkeypatch):
    from cbrain_studio.app.recording import DeviceRecorder
    def fail(*_):raise OSError('disk full')
    u=unit();ready(u);e=Experiment(tmp_path,'test',[u]);monkeypatch.setattr(DeviceRecorder,'write_block',fail)
    u.block(block(counter=64));summary=e.stop()[0]
    assert not summary['complete'] and 'disk full' in summary['error']

def test_registry_conflicts_atomic(tmp_path):
    r=Registry(tmp_path/'devices.json');r.save(1,'dongle-a','mouse A')
    with pytest.raises(RuntimeError):r.save(3,'dongle-a','mouse B')
    r.save(3,'dongle-a','mouse B',replace=True)
    assert r.preferred()=={'3':'dongle-a'}

def test_bridge_names_survive_unplug_new_devices_and_binding_updates(tmp_path):
    path=tmp_path/'devices.json';r=Registry(path)
    r.save(1,'serial-z','mouse A')
    names=r.bridge_names(['serial-z','serial-b'])
    assert names=={'serial-b':'CBRAIN_Bridge_1','serial-z':'CBRAIN_Bridge_2'}
    assert r.preferred()=={'1':'serial-z'}
    # Another app sees just one old dongle plus a newly added one. Never reuse
    # the unplugged bridge's number or renumber when serial sort order changes.
    other=Registry(path)
    assert other.bridge_names(['serial-z','serial-a'])=={'serial-a':'CBRAIN_Bridge_3','serial-z':'CBRAIN_Bridge_2'}
    other.save(2,'serial-b','mouse B')
    assert r.bridge_names(['serial-b','serial-z'])==names

def test_bridge_names_follow_usb_serial_when_ports_swap(tmp_path,monkeypatch):
    from serial.tools import list_ports
    from cbrain_studio.suite.usb import dongles
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    def port(serial,device):return SimpleNamespace(serial_number=serial,device=device,vid=0x1915,pid=0x520f)
    monkeypatch.setattr(list_ports,'comports',lambda:[port('A','/fake/1'),port('B','/fake/2')])
    first={d.serial:d.label for d in dongles()}
    monkeypatch.setattr(list_ports,'comports',lambda:[port('B','/fake/1'),port('A','/fake/2')])
    assert {d.serial:d.label for d in dongles()}==first
    assert set(first.values())=={'CBRAIN_Bridge_1','CBRAIN_Bridge_2'}

def wire(kind,p):
    body=bytes([kind])+len(p).to_bytes(2,'little')+p
    return UP+body+binascii.crc_hqx(body,0xffff).to_bytes(2,'little')

def test_decoder_every_boundary():
    payload=bytes(range(256));data=wire(0x90,payload)+wire(0x92,UP)
    for cut in range(len(data)):
        d=EnvelopeDecoder();assert d.feed(data[:cut])+d.feed(data[cut:])==[(0x90,payload),(0x92,UP)]

def test_lease_excludes_second_owner():
    lease=Lease('TEST-SUITE-EXCLUSIVE')
    try:
        with pytest.raises(RuntimeError):Lease('TEST-SUITE-EXCLUSIVE')
    finally:lease.close()

class FakeSerial:
    def __init__(self):self.data=bytearray();self.lock=threading.Lock();self.closed=False;self.commands=[]
    @property
    def in_waiting(self):
        with self.lock:return len(self.data)
    def read(self,n):
        time.sleep(.001)
        with self.lock:r=bytes(self.data[:n]);del self.data[:n];return r
    def write(self,data):
        self.commands.append(data);cmd=data[4];p=data[5:];out=b''
        if cmd==0x10:out=wire(0x91,bytes([1,1,0,0,32,15]))
        elif cmd==0x11:
            import struct
            req=p[2:6];row=req+struct.pack('<II',99,3)+b'\x00abcdef'+b'\xd3'+bytes(4)
            out=wire(0x80,bytes([cmd,0])+req)+wire(0x92,row)+wire(0x93,req+bytes([0,1,0])+bytes(4))
        elif cmd in (0x14,0x15):out=wire(0x80,bytes([cmd,0])+bytes(4))
        elif cmd==2:out=wire(0x82,bytes(12))
        with self.lock:self.data.extend(out)
        return len(data)
    def close(self):self.closed=True
    def flush(self):pass

def test_protocol_scan_connect_and_explicit_disconnect():
    serial=FakeSerial();b=USBBridge(Dongle('/fake','PROTO-TEST'),lambda:serial)
    rows=b.scan(.1);assert len(rows)==1 and rows[0].device_id==3 and rows[0].rssi==-45
    b.connect(rows[0]);b.close();assert serial.closed and any(c[4]==0x15 for c in serial.commands) and not b.thread.is_alive()

def test_recording_readiness_rejects_stale(tmp_path):
    u=unit();ready(u);u.last_time-=10
    with pytest.raises(RuntimeError):Experiment(tmp_path,'stale',[u])

def test_eight_devices_generate_distinct_verified_files(tmp_path):
    units=[unit(i) for i in range(1,9)]
    for u in units:ready(u)
    e=Experiment(tmp_path,'eight devices',[*units])
    for counter in range(64,1344,32):
        for u in units:u.block(block(u.id,counter))
    result=e.stop();assert len({r['file'] for r in result})==8
    assert all(r['complete'] and r['samples']==1280 for r in result)

def test_assign_preserves_other_uicr_settings(tmp_path,monkeypatch):
    from cbrain_studio.suite.provision import Programmer
    from cbrain_studio.app.hex_stamp import parse_hex
    import cbrain_studio.suite.provision as module
    monkeypatch.setattr(module,'state_dir',lambda:tmp_path)
    class Fake(Programmer):
        def __init__(self):
            super().__init__('12345678');self.mem=bytearray(b'\xff'*4096);self.mem[0x14:0x18]=bytes([1,2,3,4])
        def probe(self):return {'chip':'test-chip','id':int.from_bytes(self.mem[0x80:0x84],'little'),'snr':self.snr}
        def memory(self,address,size,cancellable=True):return bytes(self.mem[address-0x10001000:address-0x10001000+size])
        def run(self,args,timeout=30,cancellable=True):
            if '--eraseuicr' in args:self.mem[:]=b'\xff'*4096
            if '--program' in args:
                memory,_=parse_hex(Path(args[args.index('--program')+1]).read_text())
                for addr,value in memory.items():self.mem[addr-0x10001000]=value
            return ''
    from pathlib import Path
    p=Fake();identity=p.assign(3,'test-chip')
    assert identity['id']==3 and p.mem[0x14:0x18]==bytes([1,2,3,4])
    assert len(list((tmp_path/'uicr_backups').glob('*.hex')))==1
