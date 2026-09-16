"""A SWD memory read must never leave a successful Setup operation halted."""
from pathlib import Path
import threading
import pytest
from cbrain_studio.app.hex_stamp import emit_hex,parse_hex
from cbrain_studio.suite.provision import Programmer


class HaltedTarget(Programmer):
    def __init__(self,device=1):
        super().__init__('12345678',threading.Event())
        self.uicr=bytearray(b'\xff'*4096)
        self.uicr[0x80:0x84]=device.to_bytes(4,'little')
        self.chip=bytes.fromhex('0102030405060708')
        self.running=True
        self.advertised_id=device
        self.operations=[]

    def run(self,args,timeout=30,cancellable=True):
        if cancellable and self.cancel.is_set():raise RuntimeError('cancelled')
        self.operations.append(list(args))
        if '--memrd' in args:
            self.running=False
            address=int(args[args.index('--memrd')+1],0)
            size=int(args[args.index('--n')+1])
            data=self.chip if address==0x10000060 else self.uicr[address-0x10001000:address-0x10001000+size]
            return f'0x{address:08X}: '+' '.join(f'{int.from_bytes(data[i:i+4],"little"):08X}' for i in range(0,size,4))
        if '--eraseuicr' in args:
            self.running=False;self.uicr[:]=b'\xff'*4096
        if '--program' in args:
            self.running=False
            mem,_=parse_hex(Path(args[args.index('--program')+1]).read_text())
            for addr,value in mem.items():
                if 0x10001000<=addr<0x10002000:self.uicr[addr-0x10001000]=value
        if '--reset' in args:
            self.running=True
            self.advertised_id=int.from_bytes(self.uicr[0x80:0x84],'little')
        return ''


def assert_advertising(p,device):
    assert p.running and p.advertised_id==device
    assert p.operations[-1]==['-f','NRF52','--reset']


def test_probe_restarts_after_final_memory_read():
    p=HaltedTarget();assert p.probe()['id']==1
    assert_advertising(p,1)


def test_assign_existing_id_restarts_without_erasing(tmp_path,monkeypatch):
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    p=HaltedTarget();assert p.assign(1,p.chip.hex())['id']==1
    assert not any('--eraseuicr' in args or '--program' in args for args in p.operations)
    assert_advertising(p,1)


def test_assign_new_id_boots_with_new_advertisement(tmp_path,monkeypatch):
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    p=HaltedTarget(2);p.uicr[0x14:0x18]=b'abcd'
    assert p.assign(1,p.chip.hex())['id']==1
    assert p.uicr[0x14:0x18]==b'abcd'
    assert_advertising(p,1)


def test_flash_identity_verification_does_not_halt_final_boot(tmp_path):
    path=tmp_path/'headstage.hex';path.write_text(emit_hex({i:0 for i in range(8)}))
    p=HaltedTarget();assert p.flash(path,p.chip.hex())['id']==1
    assert_advertising(p,1)


def test_cancel_after_last_read_still_resumes():
    class CancelAfterRead(HaltedTarget):
        def memory(self,address,size,cancellable=True):
            data=super().memory(address,size,cancellable)
            if address==0x10001080:self.cancel.set()
            return data
    p=CancelAfterRead();p.probe();assert_advertising(p,1)


def test_reset_failure_is_not_reported_as_success():
    class BrokenReset(HaltedTarget):
        def run(self,args,**kwargs):
            if '--reset' in args:raise RuntimeError('reset failed')
            return super().run(args,**kwargs)
    with pytest.raises(RuntimeError,match='reset failed'):BrokenReset().probe()
