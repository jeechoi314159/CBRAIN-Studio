"""Explicit J-Link selection, bounded operations and verified identity writes."""
from __future__ import annotations
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from cbrain_studio.app.hex_stamp import emit_hex,parse_hex
from cbrain_studio.suite.runtime import state_dir

class Programmer:
    def __init__(self,snr='',cancel=None):self.snr=snr;self.cancel=cancel;self.log=[]
    def run(self,args,timeout=30,cancellable=True):
        if cancellable and self.cancel and self.cancel.is_set():raise RuntimeError('작업 취소됨')
        exe=shutil.which('nrfjprog',path=os.pathsep.join([os.environ.get('PATH',''),'/usr/local/bin','/opt/homebrew/bin']))
        if not exe:raise RuntimeError('nrfjprog를 찾을 수 없습니다. Nordic Command Line Tools를 설치하세요.')
        cmd=(['arch','-arm64'] if platform.system()=='Darwin' and platform.machine()=='arm64' else [])+[exe]
        if self.snr:cmd+=['--snr',self.snr]
        cmd+=args
        try:
            p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            end=time.monotonic()+timeout
            while True:
                try:output,_=p.communicate(timeout=.2);break
                except subprocess.TimeoutExpired:
                    if time.monotonic()>end or (cancellable and self.cancel and self.cancel.is_set()):
                        p.kill();output,_=p.communicate();raise RuntimeError('J-Link 작업 시간 초과 또는 취소. 연결을 확인하고 다시 읽으세요.')
            self.log.append('$ '+' '.join(cmd)+'\n'+output)
            if p.returncode:raise RuntimeError('\n'.join(line for line in output.splitlines() if 'reported error -256 at line' not in line)[-2500:])
            return output
        except OSError as exc:raise RuntimeError(str(exc))
    def probes(self):return re.findall(r'^\s*(\d{6,})\s*$',self.run(['--ids']),re.M)
    def memory(self,address,size,cancellable=True):
        output=self.run(['-f','NRF52','--memrd',hex(address),'--n',str(size)],cancellable=cancellable)
        mem={}
        for line in output.splitlines():
            match=re.match(r'\s*0x([0-9A-Fa-f]+):\s*(.*)',line)
            if not match:continue
            offset=int(match[1],16)
            for word in re.findall(r'\b[0-9A-Fa-f]{8}\b',match[2].split('|')[0]):
                for b in int(word,16).to_bytes(4,'little'):mem[offset]=b;offset+=1
        if any(a not in mem for a in range(address,address+size)):raise RuntimeError('칩 메모리 응답을 읽지 못했습니다.')
        return bytes(mem[a] for a in range(address,address+size))
    def probe(self):
        # nrfjprog --memrd halts the target. Finish every successful public
        # identity check with reset/run, including the no-op Assign path and
        # Flash's final readback. Nothing may read SWD again after this reset.
        try:
            chip=self.memory(0x10000060,8).hex()
            device=int.from_bytes(self.memory(0x10001080,4),'little')
        except Exception as exc:
            raise RuntimeError(str(exc)+'\n확인 중 기기가 정지했을 수 있습니다. 헤드스테이지 전원을 껐다 켠 뒤 다시 시도하세요.') from exc
        self.run(['-f','NRF52','--reset'],cancellable=False)
        return {'chip':chip,'id':device,'snr':self.snr}
    def assign(self,device,expected_chip):
        if not 1<=device<=9999:raise ValueError('ID 범위: 1–9999')
        if self.probe()['chip']!=expected_chip:raise RuntimeError('확인한 헤드스테이지와 현재 기기가 다릅니다. 다시 확인하세요.')
        original=self.memory(0x10001000,0x1000)
        if int.from_bytes(original[0x80:0x84],'little')==device:return self.probe()
        backup=state_dir()/'uicr_backups';backup.mkdir(parents=True,exist_ok=True)
        stamp=time.strftime('%Y%m%d_%H%M%S')+'_'+str(time.monotonic_ns())
        (backup/(expected_chip+'_'+stamp+'.hex')).write_text(emit_hex({0x10001000+i:b for i,b in enumerate(original)}))
        updated=bytearray(original);updated[0x80:0x84]=device.to_bytes(4,'little')
        words={0x10001000+i+j:updated[i+j] for i in range(0,4096,4) if updated[i:i+4]!=b'\xff'*4 for j in range(4)}
        with tempfile.TemporaryDirectory(prefix='cbrain-id-') as temp:
            path=Path(temp)/'identity.hex';path.write_text(emit_hex(words))
            # Once erase starts, finish restore/verification before honouring cancel.
            self.run(['-f','NRF52','--eraseuicr'],cancellable=False)
            self.run(['-f','NRF52','--program',str(path),'--verify'],cancellable=False)
            readback=self.memory(0x10001000,4096,cancellable=False)
            if readback!=bytes(updated):raise RuntimeError('UICR 전체 검증 실패. 백업: '+str(backup))
            self.run(['-f','NRF52','--reset'],cancellable=False)
        return {'chip':expected_chip,'id':device,'snr':self.snr}
    def flash(self,path,expected_chip):
        before=self.probe()
        if before['chip']!=expected_chip:raise RuntimeError('기기가 바뀌었습니다. 다시 확인하세요.')
        mem,_=parse_hex(Path(path).read_text())
        if not mem or any(a>=0x80000 for a in mem) or min(mem)!=0:raise RuntimeError('nRF52832 헤드스테이지용 HEX가 아닙니다. 동글 HEX는 Programmer에서 설치하세요.')
        self.run(['-f','NRF52','--program',str(path),'--sectorerase','--verify'],timeout=120,cancellable=False)
        self.run(['-f','NRF52','--reset'],cancellable=False)
        after=self.probe()
        if after['id']!=before['id']:raise RuntimeError('펌웨어 설치 후 ID가 달라졌습니다.')
        return after
