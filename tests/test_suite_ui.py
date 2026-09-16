import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox
from PySide6.QtCore import Qt
from cbrain_studio.suite.ui_common import configure
from cbrain_studio.suite.studio import Studio
from cbrain_studio.suite.setup import Setup,base_hex
from cbrain_studio.suite.builder import Builder
from cbrain_studio.suite.runtime import Experiment
from test_suite import unit,ready,candidate,block

@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([]);configure(app);return app

def test_all_windows_start_and_no_record_before_connection(app,tmp_path,monkeypatch):
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    for cls in [Studio,Setup,Builder]:
        w=cls();w.show();app.processEvents()
        if isinstance(w,Studio):assert not w.start.isEnabled()
        if isinstance(w,Setup):assert not w.assign_btn.isEnabled() and base_hex().is_file()
        w.quit_btn.click();app.processEvents();assert not w.isVisible()

def test_sensor_fault_locks_save_record_and_calibration(app,tmp_path,monkeypatch):
    from test_sample_source import saw
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    for cls in [Setup,Studio,Builder]:
        w=cls();u=unit();u.block(saw());w.connections.units=[u]
        w.plot.set_units([u]);w.update_controls();w.show();app.processEvents()
        control=w.save_btn if cls==Setup else w.start if cls==Studio else w.calibrate_btn
        assert not control.isEnabled()
        w.close()

def test_discovery_duplicates_are_disabled(app,tmp_path,monkeypatch):
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    w=Studio();c=w.connections;c.display([candidate(1),candidate(1,'B',b'abcdefg')])
    assert not c.choices[1].isEnabled()
    c.choices[1].click();assert c.selected_ids()==[] and not c.connect_btn.isEnabled()
    w.close()

def test_setup_visible_selection_connects_only_the_chosen_device(app,tmp_path,monkeypatch):
    import threading
    from types import SimpleNamespace
    from PySide6.QtTest import QTest
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    w=Setup();w.tabs.setCurrentIndex(1);c=w.connections
    rows=[candidate(1,'A'),candidate(2,'B')];calls=[]
    for row in rows:
        bridge=SimpleNamespace(dongle=row.dongle,connect=lambda chosen:calls.append(chosen.device_id),close=lambda:None,check_events=lambda:None)
        c.pool.bridges[row.dongle.serial]=bridge
    c.pool.candidates=rows
    monkeypatch.setattr(c.pool,'refresh_selected',lambda ids,*_:calls.append(('selected',tuple(ids))))
    monkeypatch.setattr(w,'work',lambda text,fn,done:done(fn(threading.Event())))
    c.display(rows);w.show();app.processEvents()
    # Editing names and dongles is distinct from selecting a target. The
    # action stays disabled until an actual, visible selection is made.
    c.table.cellWidget(0,2).setText('test1');c.table.cellWidget(1,2).setText('test2')
    assert not c.connect_btn.isEnabled()
    QTest.mouseClick(c.choices[1],Qt.LeftButton)
    QTest.mouseClick(c.choices[2],Qt.LeftButton)
    assert c.selected_ids()==[2] and not c.choices[1].isChecked()
    assert 'CBRAIN_2' in c.connect_btn.text() and '선택됨' in c.choices[2].text()
    QTest.mouseClick(c.connect_btn,Qt.LeftButton)
    assert calls==[('selected',(2,)),2] and [u.id for u in c.units]==[2]
    c.release();w.close()

def test_studio_row_selection_supports_multiple_devices_and_rescan_clears_it(app,tmp_path,monkeypatch):
    from PySide6.QtTest import QTest
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    w=Studio();c=w.connections;rows=[candidate(1,'A'),candidate(3,'B')]
    c.display(rows);w.show();app.processEvents()
    QTest.mouseClick(c.choices[1],Qt.LeftButton)
    point=c.table.visualItemRect(c.table.item(1,1)).center()
    QTest.mouseClick(c.table.viewport(),Qt.LeftButton,pos=point)
    assert c.selected_ids()==[1,3] and '2개 연결' in c.connect_btn.text()
    c.display(rows);assert c.selected_ids()==[] and not c.connect_btn.isEnabled()
    w.close()

def test_bridge_choice_keeps_serial_identity_and_shows_actual_assignment(app,tmp_path,monkeypatch):
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path))
    w=Studio();c=w.connections;c.display([candidate(1,'B'),candidate(1,'A')])
    combo=c.table.cellWidget(0,3)
    assert [combo.itemText(i) for i in range(combo.count())]==['자동 배정','CBRAIN_Bridge_1','CBRAIN_Bridge_2']
    assert [combo.itemData(i) for i in (1,2)]==['A','B']
    from cbrain_studio.suite.runtime import Unit
    from cbrain_studio.suite.usb import Dongle
    from types import SimpleNamespace
    u=Unit(SimpleNamespace(dongle=Dongle('/fake/B','B','CBRAIN_Bridge_2')),candidate(1,'B'))
    c.connected([u]);assert combo.currentText()=='CBRAIN_Bridge_2' and combo.currentData()=='B'
    assert 'USB 일련번호: B' in combo.toolTip()
    w.close()

def test_recording_locks_identity_and_folder(app,tmp_path,monkeypatch):
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path/'state'))
    w=Studio();u=unit();ready(u);w.connections.units=[u];w.changed();w.update_controls();assert w.start.isEnabled()
    w.experiment=Experiment(tmp_path,'ui',[u]);w.update_controls()
    assert w.connections.locked and not w.folder.isEnabled() and not w.start.isEnabled()
    u.block(block(counter=64));w.experiment.stop();w.close()

def test_builder_generates_valid_hex_and_metadata(app,tmp_path,monkeypatch):
    from cbrain_studio.app.fw_config import FwConfig,CB_CONFIG_ADDR,BLOB_SIZE
    from cbrain_studio.app.hex_stamp import read_region
    monkeypatch.setenv('CBRAIN_STATE_DIR',str(tmp_path/'state'))
    path=tmp_path/'generated.hex';monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *a,**k:(str(path),'Firmware'))
    w=Builder();w.generate();assert path.is_file() and path.with_suffix('.json').is_file()
    cfg=FwConfig.from_bytes(read_region(path.read_text(),CB_CONFIG_ADDR,BLOB_SIZE));assert cfg.ch_map==[0,1,2,3] and cfg.stream_ble
    w.close()
