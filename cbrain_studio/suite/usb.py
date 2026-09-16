"""One reader per dongle. All BLE discovery occurs in the nRF bridge."""
from __future__ import annotations
import binascii
import hashlib
import json
import os
import queue
import struct
import tempfile
import threading
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

MAGIC = b'\xcb\x1d\x70\x2a'
UP = b'\xcb\x1d\x70\x2b'

@dataclass(frozen=True)
class Dongle:
    port: str
    serial: str
    name: str = ''
    @property
    def label(self):
        return self.name or '동글 '+self.serial[-8:]

@dataclass
class Candidate:
    dongle: Dongle
    device_id: int
    token: int
    address: bytes
    rssi: int
    seen: float


def dongles():
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if (p.vid,p.pid)==(0x1915,0x520f)]
    serials = [p.serial_number for p in ports]
    if any(not s or serials.count(s)>1 for s in serials):
        raise RuntimeError('동글 USB 일련번호가 없거나 중복됩니다. 동글을 하나씩 확인하세요.')
    from cbrain_studio.suite.runtime import Registry
    names=Registry().bridge_names(serials)
    return sorted([Dongle(p.device,p.serial_number,names[p.serial_number]) for p in ports],key=lambda d:int(d.name.rsplit('_',1)[1]))


class EnvelopeDecoder:
    def __init__(self): self.buf=bytearray();self.bad=0
    def feed(self,data):
        self.buf.extend(data);result=[]
        while len(self.buf)>=7:
            pos=self.buf.find(UP)
            if pos<0: del self.buf[:-3];break
            if pos: del self.buf[:pos]
            if len(self.buf)<7: break
            n=int.from_bytes(self.buf[5:7],'little')
            if n>256: self.bad+=1;del self.buf[0];continue
            if len(self.buf)<n+9: break
            frame=bytes(self.buf[:n+9])
            if binascii.crc_hqx(frame[4:-2],0xffff)!=int.from_bytes(frame[-2:],'little'):
                self.bad+=1;del self.buf[0];continue
            del self.buf[:n+9];result.append((frame[4],frame[7:-2]))
        return result


APP_NAMES={'device_setup.py':'CBRAIN Device Setup','studio_gui.py':'CBRAIN Studio','fwbuilder_app.py':'CBRAIN Firmware Builder'}

def process_app_name():
    if getattr(sys,'frozen',False):return Path(sys.executable).name
    return APP_NAMES.get(Path(sys.argv[0]).name,'CBRAIN 프로그램')

def lease_owner(raw):
    """Read new owner metadata and legacy PID-only locks without taking ownership."""
    try:
        doc=json.loads(raw)
        if isinstance(doc,dict):
            pid=int(doc['pid']);app=doc.get('app','')
            if app in APP_NAMES.values():return f'{app} (PID {pid})'
        else:pid=int(doc)
        if pid<=0:return '다른 CBRAIN 앱'
        # Old apps wrote only a PID. Resolve a name when the OS permits it.
        import subprocess
        try:
            command=subprocess.run(['ps','-p',str(pid),'-o','comm='],capture_output=True,text=True,timeout=1).stdout.strip()
            name=Path(command).name
            if name in APP_NAMES.values():return f'{name} (PID {pid})'
        except (OSError,subprocess.TimeoutExpired):pass
        return f'다른 CBRAIN 앱 (PID {pid})'
    except (ValueError,TypeError,KeyError):return '다른 CBRAIN 앱'

class DongleBusy(RuntimeError):
    def __init__(self,owner):
        self.owner=owner
        super().__init__(f'{owner}에서 사용 중입니다. 해당 앱에서 연결 해제 또는 종료 후 다시 검색하세요.')

class Lease:
    """Advisory process lock plus pyserial's exclusive port open."""
    def __init__(self,serial):
        import fcntl
        path=Path(tempfile.gettempdir())/('cbrain-usb-'+hashlib.sha256(serial.encode()).hexdigest()[:20]+'.lock')
        self.file=path.open('a+')
        try: fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.seek(0);raw=self.file.read(1024);self.file.close();raise DongleBusy(lease_owner(raw))
        self.file.seek(0);self.file.truncate();self.file.write(json.dumps({'pid':os.getpid(),'app':process_app_name()}));self.file.flush()
    def close(self): self.file.close()


class USBBridge:
    def __init__(self,dongle,serial_factory=None):
        self.dongle=dongle;self.error='';self.on_data=None;self.on_error=None
        self.decoder=EnvelopeDecoder();self.events=queue.Queue(maxsize=1024)
        self.lock=threading.RLock();self.write_lock=threading.Lock();self.running=False
        self.lease=None;self.ser=None;self.thread=None;self.firmware='?'
        try:
            self.lease=Lease(dongle.serial)
            if serial_factory: self.ser=serial_factory()
            else:
                import serial
                self.ser=serial.Serial(dongle.port,1_000_000,timeout=.1,write_timeout=2,exclusive=True)
            self.running=True
            self.thread=threading.Thread(target=self._read,daemon=True,name='usb-'+dongle.label);self.thread.start()
            self._negotiate()
        except Exception:
            self._release();raise
    def _read(self):
        try:
            while self.running:
                chunk=self.ser.read(min(self.ser.in_waiting or 1,8192))
                before=self.decoder.bad
                for kind,p in self.decoder.feed(chunk):
                    if kind==0x90:
                        if self.on_data: self.on_data(p)
                    else: self.events.put_nowait((kind,p))
                if self.decoder.bad!=before:
                    raise RuntimeError('동글 USB 프레임 검사 실패 — 연결을 다시 확인하세요.')
        except Exception as exc:
            if self.running:
                self.error=str(exc)
                if self.on_error: self.on_error(self.error)
            self.running=False
    def send(self,cmd,payload=b''):
        with self.write_lock:
            if not self.running: raise RuntimeError(self.error or '동글 연결이 닫혔습니다.')
            frame=MAGIC+bytes([cmd])+payload
            if self.ser.write(frame)!=len(frame): raise RuntimeError('USB 명령 쓰기 실패')
    def control(self,payload):
        with self.write_lock:
            if not self.running: raise RuntimeError(self.error or '동글 연결 끊김')
            if self.ser.write(payload)!=len(payload):raise RuntimeError('기기 명령 쓰기 실패')
    def clear(self):
        while True:
            try:self.events.get_nowait()
            except queue.Empty:return
    def wait(self,predicate,timeout=3):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if self.error:raise RuntimeError(self.error)
            try:event=self.events.get(timeout=min(.1,max(.001,end-time.monotonic())))
            except queue.Empty:continue
            if event[0]==0x80 and len(event[1])==6 and event[1][1]:
                cmd,status,detail=struct.unpack('<BBI',event[1])
                text={1:'동글 사용 중',2:'번호 중복 또는 잘못된 요청',3:'검색 결과 만료 — 다시 검색하세요',4:'펌웨어 미지원',5:'무선 연결 실패/시간 초과',6:'동글 명령 대기열 초과'}
                raise RuntimeError(f'{text.get(status,"동글 오류")} (명령 {cmd:02x}, {detail})')
            if predicate(*event):return event[1]
        raise TimeoutError('동글 응답 시간 초과')
    def _negotiate(self):
        with self.lock:
            self.send(1,bytes(4))
            for _ in range(12):
                self.send(0x10)
                try:p=self.wait(lambda t,p:t==0x91,.5)
                except TimeoutError:continue
                if len(p)!=6 or p[0]!=1 or p[5]&15!=15:raise RuntimeError('지원하지 않는 동글 검색 프로토콜')
                self.firmware='.'.join(map(str,p[1:4]));return
            raise RuntimeError('동글 검색 펌웨어 응답 없음. 새 dongle_bridge.hex 설치와 USB 상태를 확인하세요.')
    def scan(self,seconds=8,cancel=None):
        with self.lock:
            self.clear();request=time.monotonic_ns()&0xffffffff
            self.send(0x11,struct.pack('<HI',int(seconds*1000),request))
            result=[];deadline=time.monotonic()+seconds+5;stopped=False
            while time.monotonic()<deadline:
                if cancel and cancel.is_set() and not stopped:self.send(0x12);stopped=True
                try:p=self.wait(lambda t,p:t in (0x92,0x93),.3)
                except TimeoutError:continue
                if int.from_bytes(p[:4],'little')!=request:continue
                if len(p)==24:
                    token,device=struct.unpack('<II',p[4:12])
                    age=int.from_bytes(p[20:],'little')/1000
                    result.append(Candidate(self.dongle,device,token,p[12:19],struct.unpack('b',p[19:20])[0],time.monotonic()-age))
                elif len(p)==11:
                    if p[6] or int.from_bytes(p[7:],'little'):raise RuntimeError('검색 결과 넘침/USB 손실 — 동글을 다시 꽂고 재검색하세요.')
                    if len(result)!=p[5]:raise RuntimeError('검색 목록 일부를 받지 못했습니다. 다시 검색하세요.')
                    return [] if stopped else result
            raise TimeoutError('기기 검색 종료 응답 없음')
    def connect(self,candidate):
        with self.lock:
            self.clear();self.send(0x14,struct.pack('<II',candidate.token,candidate.device_id))
            self.wait(lambda t,p:t==0x80 and len(p)==6 and p[0]==0x14)
    def check_events(self):
        """Nonblocking monitor after connection; errors are never silently discarded."""
        with self.lock:
            while True:
                try:kind,p=self.events.get_nowait()
                except queue.Empty:return
                if kind==0x80 and len(p)==6 and p[1]:
                    raise RuntimeError(f'동글 연결 오류 (status {p[1]})')
    def close(self):
        failure=''
        try:
            if self.running and self.firmware!='?':
                with self.lock:
                    self.clear();self.send(0x15)
                    self.wait(lambda t,p:t==0x80 and p[0]==0x15)
                    deadline=time.monotonic()+4
                    while True:
                        self.send(2);p=self.wait(lambda t,p:t==0x82,1)
                        if len(p)>=5 and not p[4]&1:break
                        if time.monotonic()>deadline:raise TimeoutError('BLE 연결 해제 확인 실패 — 동글을 다시 꽂으세요.')
                        time.sleep(.1)
                    self.send(1,bytes(4));self.ser.flush()
        except Exception as exc: failure=str(exc)
        finally:self._release()
        if failure:raise RuntimeError(failure)
    def _release(self):
        self.running=False
        if self.ser:
            self.ser.close()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(2)
        if self.lease:self.lease.close();self.lease=None


def allocate(candidates,device_ids,preferred=None):
    """Bipartite matching; reject conflicting identities before allocation."""
    preferred=preferred or {};options={}
    for device in device_ids:
        rows=[c for c in candidates if c.device_id==device]
        if len({c.address for c in rows})>1:raise RuntimeError(f'CBRAIN_{device}: 같은 번호의 다른 기기가 발견되었습니다.')
        options[device]=sorted(rows,key=lambda c:(c.dongle.serial==preferred.get(str(device)),c.rssi),reverse=True)
    ordered=sorted(device_ids,key=lambda d:len(options[d]))
    def walk(i,used,chosen):
        if i==len(ordered):return chosen
        device=ordered[i]
        for c in options[device]:
            if c.dongle.serial not in used:
                found=walk(i+1,used|{c.dongle.serial},{**chosen,device:c})
                if found is not None:return found
        return None
    result=walk(0,set(),{})
    if result is None:raise RuntimeError('선택 기기를 모두 연결할 동글이 부족하거나 해당 동글에서 기기가 발견되지 않았습니다.')
    return result


class Pool:
    def __init__(self):self.bridges={};self.candidates=[];self.errors=[];self.detected=[];self.busy={}
    def scan(self,cancel=None,seconds=8):
        self.close();self.errors=[];self.detected=[];self.busy={};results=[]
        devices=dongles();self.detected=devices
        if not devices:raise RuntimeError('USB 동글이 없습니다. 연결 후 다시 검색하세요.')
        def one(d):
            bridge=USBBridge(d)
            try:return bridge,bridge.scan(seconds,cancel)
            except Exception:
                try:bridge.close()
                except Exception:pass
                raise
        with ThreadPoolExecutor(max_workers=min(8,len(devices))) as ex:
            jobs={ex.submit(one,d):d for d in devices[:8]}
            for future in as_completed(jobs):
                try:
                    bridge,rows=future.result();self.bridges[bridge.dongle.serial]=bridge;results.extend(rows)
                except DongleBusy as exc:
                    d=jobs[future];self.busy[d.serial]=f'{d.label} → {exc.owner}';self.errors.append(f'{d.label}: {exc}')
                except Exception as exc:self.errors.append(f'{jobs[future].label}: {exc}')
        self.candidates=results
        return results
    def refresh_selected(self,ids,preferred=None,cancel=None):
        # Search again before accepting tokens, rather than using expired GUI rows.
        rows=[]
        with ThreadPoolExecutor(max_workers=max(1,len(self.bridges))) as ex:
            jobs=[ex.submit(b.scan,3,cancel) for b in self.bridges.values()]
            for future in jobs:rows.extend(future.result())
        if cancel and cancel.is_set():raise RuntimeError('연결 취소됨')
        self.candidates=rows
        return allocate(rows,ids,preferred)
    def close(self):
        errors=[]
        for bridge in list(self.bridges.values()):
            try:bridge.close()
            except Exception as exc:errors.append(str(exc))
        self.bridges.clear();self.candidates=[]
        if errors:raise RuntimeError('\n'.join(errors))
