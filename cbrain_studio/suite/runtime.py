"""Validated acquisition, bounded background recording and shared device registry."""
from __future__ import annotations
import csv
import json
import os
import queue
import shutil
import threading
import time
import uuid
from collections import deque
from pathlib import Path
import numpy as np
from cbrain_studio.core.packet import Decoder
from cbrain_studio.app.display_buffer import DisplayBuffer
from cbrain_studio.app.recording import DeviceRecorder
from cbrain_studio.suite import VERSION
from cbrain_studio.suite.source import SOURCE_VALID,SENSOR_FAULT,RHD_UV_PER_LSB,FAULTS,legacy_test_pattern
from cbrain_studio.core.types import UV_PER_LSB


def state_dir():
    return Path(os.environ.get('CBRAIN_STATE_DIR',str(Path.home()/'Library/Application Support/CBRAIN')))

def atomic_json(path,doc):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('x') as f:json.dump(doc,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()

class Registry:
    def __init__(self,path=None):self.path=Path(path) if path else state_dir()/'devices.json'
    def read(self):
        if not self.path.exists():return {'version':1,'devices':{}}
        doc=json.loads(self.path.read_text())
        if doc.get('version')!=1:raise RuntimeError('장치 등록부 버전을 지원하지 않습니다.')
        return doc
    def preferred(self):return {k:v['dongle_serial'] for k,v in self.read()['devices'].items()}
    def bridge_names(self,serials):
        """Allocate once per USB serial; keep unplugged bridges reserved."""
        import fcntl
        serials=sorted(set(serials))
        if not serials:return {}
        if any(not s for s in serials):raise ValueError('동글 일련번호가 필요합니다.')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            doc=self.read();entries=doc.setdefault('dongles',{})
            numbers=[entry.get('number') for entry in entries.values()]
            if any(type(n) is not int or n<1 for n in numbers) or len(set(numbers))!=len(numbers):
                raise RuntimeError('저장된 동글 이름에 중복 또는 잘못된 번호가 있습니다.')
            next_number=max(numbers,default=0)+1;changed=False
            for serial in serials:
                if serial not in entries:
                    entries[serial]={'number':next_number};next_number+=1;changed=True
            if changed:atomic_json(self.path,doc)
            return {s:f'CBRAIN_Bridge_{entries[s]["number"]}' for s in serials}
    def save(self,device,serial,label,chip=None,replace=False):
        import fcntl
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            doc=self.read();entries=doc['devices']
            collisions=[k for k,v in entries.items() if (k==str(device) and v['dongle_serial']!=serial) or (k!=str(device) and v['dongle_serial']==serial)]
            if collisions and not replace:raise RuntimeError('이미 저장된 다른 기기–동글 조합이 있습니다. 조합 교체를 확인하세요.')
            for k in collisions:del entries[k]
            entries[str(device)]={'dongle_serial':serial,'label':label,'chip':chip,'verified_at':time.time(),'app_version':VERSION}
            atomic_json(self.path,doc)


class Unit:
    def __init__(self,bridge,candidate,label=''):
        self.bridge=bridge;self.candidate=candidate;self.id=candidate.device_id;self.label=label
        self.decoder=Decoder();self.buffer=DisplayBuffer();self.lock=threading.RLock()
        self.blocks=0;self.samples=0;self.gaps=0;self.bad=0;self.last_time=0.;self.first_time=0.
        self.error='';self.last_block=None;self.expected=None;self.unwrapped=0;self.anchor=None
        self.history=deque();self.writer=None;self.listeners=[];self.connected_at=time.monotonic()
        self.signature=None;self.last_gap_time=0.;bridge.on_data=self.feed;bridge.on_error=self.fail
        self.source='unverified';self.uv_per_lsb=UV_PER_LSB
    def fail(self,error):
        with self.lock:
            self.error=str(error)
            if self.writer:self.writer.fail(self.error)
    def feed(self,data):
        try:
            res=self.decoder.decode(data);self.bad=self.decoder.frames_bad
            for block in res.blocks:self.block(block)
        except Exception as exc:self.fail(exc)
    def block(self,b):
        with self.lock:
            if self.error:return
            if b.device_id!=self.id:self.fail(f'번호 불일치: 선택 {self.id} / 수신 {b.device_id}');return
            if b.flags & SENSOR_FAULT:
                self.source='sensor_fault'
                self.fail('센서 오류 · '+FAULTS.get((b.flags >> 4) & 7,'증폭기 확인 실패'));return
            if legacy_test_pattern(b):
                self.source='synthetic'
                self.fail('시험 신호 감지 · 실제 센서 측정 아님 — 헤드스테이지 점검 필요');return
            source='RHD2216' if b.flags & SOURCE_VALID and b.enc==0 else 'unverified'
            if self.source=='RHD2216' and source!='RHD2216':
                self.fail('센서 확인 정보 소실 — 기록 중단');return
            if self.blocks and source!=self.source:
                self.fail('센서 출처 변경 — 연결을 다시 시작하세요.');return
            self.source=source
            self.uv_per_lsb=RHD_UV_PER_LSB if source=='RHD2216' else UV_PER_LSB
            signature=(b.sr_hz,b.channel_count,tuple(b.order),b.enc)
            if self.signature and signature!=self.signature:self.fail('수신 형식 변경 — 연결을 다시 시작하세요.');return
            missing=0
            if self.expected is not None:
                missing=(b.first_counter-self.expected)&0xffffffff
                if missing>=0x80000000:self.fail('기기 재시작/카운터 역행 — 기존 기록과 분리해야 합니다.');return
                self.unwrapped+=missing
            else:
                self.unwrapped=b.first_counter;self.anchor=(time.time(),self.unwrapped)
                self.buffer=DisplayBuffer(b.sr_hz,max_seconds=60)
            self.signature=signature
            self.expected=(b.first_counter+b.n_samples)&0xffffffff
            counters=np.arange(self.unwrapped,self.unwrapped+b.n_samples,dtype=np.uint64)
            timestamps=self.anchor[0]+(counters.astype(float)-self.anchor[1])/b.sr_hz
            self.unwrapped+=b.n_samples;self.samples+=b.n_samples;self.blocks+=1;self.gaps+=missing
            self.last_time=time.monotonic()
            if missing:self.last_gap_time=self.last_time
            self.first_time=self.first_time or self.last_time
            self.last_block=b;self.history.append((self.last_time,self.samples))
            while len(self.history)>2 and self.history[1][0]<self.last_time-2:self.history.popleft()
            if self.writer:self.writer.submit(b,counters,timestamps,missing)
            self.buffer.push(b)
            if self.source=='RHD2216':
                for listener in list(self.listeners):listener(b)
    @property
    def ready(self):return self.source=='RHD2216' and not self.error and self.bad==0 and time.monotonic()-self.last_gap_time>=2 and self.blocks>1 and time.monotonic()-self.last_time<2 and self.last_time-self.first_time>=2
    @property
    def rate(self):
        with self.lock:
            if not self.history or time.monotonic()-self.last_time>2:return 0.
            a,b=self.history[0],self.history[-1]
            return (b[1]-a[1])/(b[0]-a[0]) if b[0]>a[0] else 0.
    @property
    def status(self):
        if self.error:return self.error
        if self.bad:return '불량 프레임 수신 — 다시 연결하세요'
        if self.last_time and time.monotonic()-self.last_time>2:return '신호 끊김'
        if not self.blocks and time.monotonic()-self.connected_at>35:return '연결 시간 초과'
        if self.blocks and self.source=='unverified':return '데이터 수신 · 센서 확인 불가 — 헤드스테이지 펌웨어 업데이트 필요'
        return 'RHD2216 확인 · 수신 중' if self.ready else '연결 확인 중'


class Writer:
    def __init__(self,folder,unit):
        self.unit=unit;self.folder=Path(folder);self.queue=queue.Queue(512)
        self.lock=threading.Lock();self.finished=False;self.stop_event=threading.Event()
        self.ready=threading.Event();self.error='';self.written=0;self.flush_time=0.;self.path=None
        self.thread=threading.Thread(target=self._run,daemon=True,name=f'writer-{unit.id}');self.thread.start()
        if not self.ready.wait(10):self.fail('파일 준비 시간 초과');self.stop_event.set();raise RuntimeError(self.error)
        if self.error:self.thread.join(2);raise RuntimeError(self.error)
    def fail(self,error):
        with self.lock:
            if not self.error:self.error=str(error)
    def submit(self,b,counters,timestamps,missing):
        if self.error or self.finished:return
        try:self.queue.put_nowait((b,counters,timestamps,missing))
        except queue.Full:self.fail('디스크 쓰기 지연: 기록 대기열 초과')
    def _run(self):
        rec=None
        try:
            b=self.unit.last_block
            rec=DeviceRecorder(str(self.folder),self.unit.id,metadata={'software_version':VERSION,'dongle_serial':self.unit.bridge.dongle.serial,'dongle_name':self.unit.bridge.dongle.label,'label':self.unit.label,'bridge_version':self.unit.bridge.firmware,'sample_source':self.unit.source,'uv_per_lsb':self.unit.uv_per_lsb,'adc_fullscale_mv':self.unit.uv_per_lsb*32768/1000})
            self.path=rec.open(b.channel_count,b.sr_hz,b.order)
            rec._h5.flush();self.ready.set();last_flush=time.monotonic()
            while not self.stop_event.is_set() or not self.queue.empty():
                try:b,counters,timestamps,missing=self.queue.get(timeout=.1)
                except queue.Empty:continue
                if self.error:continue
                if b.device_id!=self.unit.id:raise RuntimeError('파일 ID와 데이터 ID 불일치')
                if missing:rec.add_discontinuity(int(counters[0]),float(timestamps[0]),missing)
                rec.write_block(b,counters,timestamps);self.written=rec.n_written
                if time.monotonic()-last_flush>=1:
                    if shutil.disk_usage(self.folder).free<32*1024*1024:raise OSError('디스크 여유 공간 부족')
                    rec._h5.flush();self.flush_time=time.time();last_flush=time.monotonic()
            rec.close()
        except Exception as exc:
            self.fail(exc)
            if rec and rec._h5:
                try:rec._h5.close()
                except Exception:pass
        finally:self.ready.set();self.finished=True
    def close(self):
        self.stop_event.set();self.thread.join(30)
        if self.thread.is_alive():self.fail('저장 마무리 시간 초과');return self.summary(False)
        valid=False
        if self.path:
            try:
                import h5py
                with h5py.File(self.path,'r') as f:
                    valid=(f.attrs['device_id']==self.unit.id and f['samples'].shape[1]==self.written and all(len(f[k])==self.written for k in ['sample_counter','timestamps','led_state']) and int(f.attrs.get('n_samples',-1))==self.written)
                if not valid:self.fail('파일 재열기 검증 실패')
            except Exception as exc:self.fail(f'파일 검증 실패: {exc}')
        return self.summary(valid)
    def summary(self,valid):
        return {'device_id':self.unit.id,'file':self.path,'samples':self.written,'verified':valid,'error':self.error,'complete':valid and self.written>0 and not self.error,'gaps':self.unit.gaps,'bad':self.unit.bad}


class Experiment:
    def __init__(self,root,name,units):
        if not units or not all(u.ready for u in units):raise RuntimeError('선택한 모든 기기가 수신 준비를 마쳐야 합니다.')
        if len({u.id for u in units})!=len(units):raise RuntimeError('중복 기기 ID')
        root=Path(root).expanduser();root.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(root).free<256*1024*1024:raise RuntimeError('저장 공간이 부족합니다. 최소 256 MB 필요.')
        self.folder=root/(time.strftime('session_%Y-%m-%d_%H-%M-%S_')+uuid.uuid4().hex[:8]);self.folder.mkdir()
        self.units=units;self.writers=[];self.active=False;self.events=[];self.started=time.time();self.last_log=0.
        self.meta={'version':VERSION,'name':name,'started_at':self.started,'closed':False,'devices':[{'id':u.id,'label':u.label,'dongle_serial':u.bridge.dongle.serial,'dongle_name':u.bridge.dongle.label,'firmware':u.bridge.firmware} for u in units]}
        self.csv=None
        try:
            for unit in units:self.writers.append(Writer(self.folder,unit))
            self.csv=(self.folder/'integrity.csv').open('x',newline='');self.log=csv.writer(self.csv)
            self.log.writerow(['elapsed_s','device_id','received_samples','written_samples','missing_samples','bad_frames','status'])
            atomic_json(self.folder/'session.json',self.meta)
            if not all(u.ready for u in units):raise RuntimeError('파일 준비 중 기기 연결 상태가 바뀌었습니다.')
            for unit,writer in zip(units,self.writers):
                with unit.lock:unit.writer=writer
            self.active=True
        except Exception:
            for writer in self.writers:writer.close()
            if self.csv:self.csv.close()
            self.meta['startup_failed']=True;atomic_json(self.folder/'session.json',self.meta);raise
    def tick(self):
        if not self.active or time.monotonic()-self.last_log<1:return
        self.last_log=time.monotonic()
        try:
            for u,w in zip(self.units,self.writers):self.log.writerow([round(time.time()-self.started,3),u.id,u.samples,w.written,u.gaps,u.bad,w.error or u.status])
            self.csv.flush()
        except Exception as exc:
            for w in self.writers:w.fail(f'상태 기록 실패: {exc}')
    def event(self,label):
        self.events.append({'elapsed_s':time.time()-self.started,'label':label})
        atomic_json(self.folder/'events.json',self.events)
    def stop(self):
        if not self.active:return []
        for u in self.units:
            with u.lock:u.writer=None
        summaries=[w.close() for w in self.writers];self.active=False
        self.csv.close();self.meta.update(closed=True,ended_at=time.time(),files=summaries,events=self.events)
        atomic_json(self.folder/'session.json',self.meta)
        return summaries
