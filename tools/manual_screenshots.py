"""Render manual examples from the real UI without using hardware or user data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import h5py
import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRect
from cbrain_studio.suite.ui_common import configure
from cbrain_studio.suite.setup import Setup
from cbrain_studio.suite.studio import Studio
from cbrain_studio.suite.viewer import Viewer
from cbrain_studio.suite.saved_recording import inspect, read_view
from cbrain_studio.suite import usb


def main():
    root = Path(__file__).resolve().parents[1]
    out = root/'docs/assets/manual'
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cbrain-docs-') as state:
        os.environ['CBRAIN_STATE_DIR'] = state
        def no_hardware(*args, **kwargs):
            raise RuntimeError('Manual rendering does not access hardware')
        usb.dongles = no_hardware
        app = QApplication([])
        configure(app)
        def capture(window, name):
            window.resize(1440, 980)
            window.message('문서용 화면 예시 · 실제 장치와 연결하거나 녹화하지 않았습니다.')
            window.show();app.processEvents()
            height={'setup-prepare.png':670,'setup-select.png':535,'studio-select.png':560}.get(name,window.height())
            assert window.grab(QRect(0,0,window.width(),height)).save(str(out/name))
            window.close();app.processEvents()
        w = Setup()
        w.hex.setText('배포 폴더/app/firmware/headstage_common.hex')
        capture(w, 'setup-prepare.png')
        rows = [usb.Candidate(usb.Dongle('문서용 USB 예시',f'DOC000{i}',f'CBRAIN_Bridge_{i}'),
                              i, i, bytes([i])*7, -40-i*3, time.monotonic()) for i in (1,2)]
        for cls, name in [(Setup,'setup-select.png'),(Studio,'studio-select.png')]:
            w = cls()
            if isinstance(w, Setup):w.tabs.setCurrentIndex(1)
            else:
                w.name.setText('연결 확인 예시')
                w.folder.setText('~/CBRAIN_recordings/')
            w.connections.display(rows)
            for i in (1,2):
                w.connections.table.cellWidget(i-1,3).setCurrentIndex(i)
            w.connections.choices[1].click()
            if isinstance(w,Studio):w.connections.choices[2].click()
            capture(w,name)
        p=Path(state)/'Example_Device_001.h5'
        sr=1024;n=sr*30;t=np.arange(n)/sr
        samples=np.vstack([150*np.sin(2*np.pi*(i+1)*3*t)+40*np.sin(2*np.pi*.2*t) for i in range(4)]).astype(np.int16)
        with h5py.File(p,'w') as f:
            f['samples']=samples;f['sample_counter']=np.arange(n,dtype=np.uint64)
            f['led_state']=np.zeros(n,np.uint8)
            f.attrs.update(sr_hz=sr,n_samples=n,channel_count=4,device_id=1,
                           dongle_name='CBRAIN_Bridge_1',uv_per_lsb=.195,
                           sample_source='문서용 합성 신호',led_reported=False)
        w=Viewer();r=inspect(p);w.loaded(r)
        w.file_box.blockSignals(True);w.file_box.addItem(p.name);w.file_box.blockSignals(False)
        w.opened=False;w.queue_view=lambda *_:None
        w.plot.resized.disconnect();w.debounce.stop()
        w.plot.set_frame(r,read_view(r,0,2,[0,1,2,3],1400))
        w.span.blockSignals(True);w.span.setCurrentIndex(w.span.findData(2));w.span.blockSignals(False)
        w.update_controls();capture(w,'viewer-example.png')
    print('Rendered four manual examples; no hardware or user recordings accessed')


if __name__=='__main__':main()
