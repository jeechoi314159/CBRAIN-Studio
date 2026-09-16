import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from cbrain_studio.suite.ui_common import configure
from cbrain_studio.suite import viewer
from cbrain_studio.suite.saved_recording import Cancelled
from test_saved_recording import make_file


@pytest.fixture(scope='module')
def app():
    app = QApplication.instance() or QApplication([])
    configure(app)
    return app


def wait_until(app, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), 'GUI did not finish its background operation'


def wait_idle(app, w):
    wait_until(app, lambda: not w.busy and w.render_job is None and not w.debounce.isActive())


def test_open_select_pan_zoom_export_and_no_hardware(app, tmp_path, monkeypatch):
    from cbrain_studio.suite import usb
    import serial
    def forbidden(*args, **kwargs):
        pytest.fail('The offline viewer must not scan or open hardware')
    monkeypatch.setattr(usb, 'dongles', forbidden)
    monkeypatch.setattr(serial, 'Serial', forbidden)
    path = make_file(tmp_path/'data.h5', np.tile(np.arange(10000, dtype=np.int16), (4, 1)))
    w = viewer.Viewer()
    try:
        w.show()
        w.open_paths([path])
        wait_idle(app, w)
        assert w.plot.frame.sample_count == 1000 and w.export_btn.isEnabled()
        assert len(w.channels) == 4 and w.recording.duration == 100
        w.next.click()
        wait_idle(app, w)
        assert w.plot.frame.start == 10
        w.zoom_in.click()
        wait_idle(app, w)
        assert w.plot.frame.span == 5
        w.slider.setValue(10000)
        wait_idle(app, w)
        assert w.plot.frame.start == 95 and w.plot.frame.sample_count == 500
        w.channels[1].click()
        wait_idle(app, w)
        assert w.plot.frame.rows == [0, 2, 3]
        w.amplitude.setCurrentIndex(1)
        assert w.plot.amplitude == 50
        w.details_btn.click()
        assert w.details.isVisible() and str(path) in w.details.toPlainText()
        wait_idle(app, w)
        exported = tmp_path/'figure.png'
        monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(exported), 'PNG'))
        w.export_btn.click()
        img = QImage(str(exported))
        assert not img.isNull() and img.width() > 1000
        w.set_channels(False)
        wait_idle(app, w)
        assert w.plot.frame.rows == []
        w.all_btn.click()
        wait_idle(app, w)
        assert w.plot.frame.rows == [0, 1, 2, 3]
    finally:
        w.close()
        wait_until(app, lambda: not w.isVisible())


def test_folder_multiple_files_switch_and_recover_after_failure(app, tmp_path):
    make_file(tmp_path/'a.h5', np.zeros((1, 10), np.int16), device_id=1)
    make_file(tmp_path/'b.hdf5', np.zeros((16, 100), np.int16), device_id=7)
    bad = tmp_path/'invalid.h5'
    w = viewer.Viewer()
    try:
        w.show()
        w.open_paths([tmp_path])
        wait_idle(app, w)
        assert w.file_box.count() == 2 and w.recording.device == 'CBRAIN_1'
        w.file_box.setCurrentIndex(1)
        wait_idle(app, w)
        assert w.recording.device == 'CBRAIN_7' and len(w.channels) == 16
        assert w.plot.minimumHeight() > 1500
        assert not w.grab().isNull()
        bad.write_bytes(b'not an HDF5 file')
        w.set_files([bad, tmp_path/'a.h5'])
        wait_idle(app, w)
        assert w.recording is None and w.plot.frame is None and w.channels == []
        assert not w.export_btn.isEnabled() and '확인 필요' in w.status.text()
        w.file_box.setCurrentIndex(1)
        wait_idle(app, w)
        assert w.recording.device == 'CBRAIN_1' and w.plot.frame.sample_count == 10
    finally:
        w.close()
        wait_until(app, lambda: not w.isVisible())


def test_latest_navigation_wins_and_close_cancels_background_read(app, tmp_path, monkeypatch):
    path = make_file(tmp_path/'data.h5', np.zeros((4, 10000), np.int16))
    original = viewer.read_view
    entered = threading.Event()
    def slow(*args):
        cancel = args[-1]
        entered.set()
        if cancel.wait(.2):
            raise Cancelled()
        return original(*args)
    monkeypatch.setattr(viewer, 'read_view', slow)
    w = viewer.Viewer()
    w.show()
    w.set_files([path])
    wait_until(app, entered.is_set)
    w.start.setValue(10)
    w.start.setValue(20)
    wait_idle(app, w)
    assert w.plot.frame.start == 20
    entered.clear()
    w.start.setValue(30)
    wait_until(app, entered.is_set)
    w.close()
    wait_until(app, lambda: not w.isVisible())
    assert w.render_job is None and not w.busy


def test_close_during_inspection_cancels_without_stale_widgets(app, tmp_path, monkeypatch):
    path = make_file(tmp_path/'data.h5')
    entered = threading.Event()
    def slow(path, cancel):
        entered.set()
        cancel.wait(3)
        raise Cancelled()
    monkeypatch.setattr(viewer, 'inspect', slow)
    w = viewer.Viewer()
    w.show()
    w.set_files([path])
    wait_until(app, entered.is_set)
    w.close()
    wait_until(app, lambda: not w.isVisible())
    assert not w.busy and w.recording is None
