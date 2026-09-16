from __future__ import annotations
import sys
import time
from pathlib import Path
from PySide6.QtWidgets import QWidget,QTabWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QSpinBox,QLineEdit,QFileDialog,QMessageBox,QGroupBox
from cbrain_studio.suite.ui_common import Window,Connections,Plot,plot_area,button,launch_studio,LogView
from cbrain_studio.suite.provision import Programmer
from cbrain_studio.suite.runtime import Registry

def base_hex():
    if getattr(sys,'frozen',False):return Path(sys._MEIPASS)/'firmware/headstage_common.hex'
    return Path(__file__).resolve().parents[2]/'app/firmware/headstage_common.hex'

class Setup(Window):
    def __init__(self):
        super().__init__('CBRAIN Device Setup','헤드스테이지에 번호를 부여하고, 사용할 동글과 연결을 확인합니다. 이미 준비된 기기는 동글 연결 탭으로 이동하세요.')
        self.identity=None;self.tabs=QTabWidget();self.layout.addWidget(self.tabs)
        prepare=QWidget();p=QVBoxLayout(prepare);p.setContentsMargins(20,18,20,18);p.setSpacing(18)
        group=QGroupBox('1  헤드스테이지 확인');g=QVBoxLayout(group)
        row=QHBoxLayout();self.probe_box=QComboBox();self.probe_box.setMinimumWidth(220);row.addWidget(QLabel('J-Link'));row.addWidget(self.probe_box,1);row.addWidget(button('J-Link 찾기',self.find));self.probe_btn=button('현재 기기 확인',self.probe,'primary');row.addWidget(self.probe_btn);g.addLayout(row)
        self.identity_label=QLabel('CBC-V3에 헤드스테이지를 안착시키고 J-Link를 연결하세요.');self.identity_label.setWordWrap(True);g.addWidget(self.identity_label);p.addWidget(group)
        group=QGroupBox('2  기기 번호');g=QHBoxLayout(group);self.id=QSpinBox();self.id.setRange(1,9999);self.id.setMinimumWidth(120);g.addWidget(QLabel('CBRAIN 번호'));g.addWidget(self.id);self.assign_btn=button('번호 저장 및 검증',self.assign);g.addWidget(self.assign_btn);g.addStretch();p.addWidget(group)
        group=QGroupBox('3  녹화 펌웨어  ·  필요할 때만');g=QVBoxLayout(group);row=QHBoxLayout();self.hex=QLineEdit(str(base_hex().resolve()));row.addWidget(self.hex,1);row.addWidget(button('HEX 선택',self.choose_hex));self.flash_btn=button('펌웨어 설치',self.flash);row.addWidget(self.flash_btn);g.addLayout(row);g.addWidget(QLabel('헤드스테이지 전용입니다. 동글 펌웨어는 nRF Connect Programmer에서 설치하세요.'));p.addWidget(group)
        self.log=LogView();self.log.setPlaceholderText('작업 결과가 여기에 표시됩니다.');self.log.setMinimumHeight(150);p.addWidget(self.log,1)
        self.tabs.addTab(prepare,'기기 번호 · 펌웨어')
        pair=QWidget();v=QVBoxLayout(pair);v.setContentsMargins(20,18,20,18);v.setSpacing(14)
        self.connections=Connections(self,single=True);self.connections.changed.connect(self.connection_changed);v.addWidget(self.connections)
        self.expected=QLabel('기존 번호가 있는 기기도 바로 검색하고 연결할 수 있습니다.');v.addWidget(self.expected)
        self.save_btn=button('연결 조합 저장 후 해제',self.save_binding,'primary');row=QHBoxLayout();row.addWidget(self.save_btn);row.addStretch();row.addWidget(button('Studio 열기',self.open_studio));v.addLayout(row)
        self.plot=Plot();v.addWidget(plot_area(self.plot),1);self.tabs.addTab(pair,'동글 연결 · 확인')
        self.probe_box.currentIndexChanged.connect(self.reset_identity);self.update_controls()
    def show_error(self,msg):
        if hasattr(self,'log'):self.log.setPlainText(msg)
        self.message('확인 필요 · '+msg.split('\n')[0][:200])
    def reset_identity(self):self.identity=None;self.update_controls()
    def find(self):
        self.work('연결된 J-Link를 찾고 있습니다…',lambda c:Programmer(cancel=c).probes(),self.found)
    def found(self,snrs):
        self.probe_box.clear();self.probe_box.addItems(snrs);self.message('J-Link를 선택하고 현재 기기를 확인하세요.' if snrs else 'J-Link가 없습니다. USB 연결을 확인하세요.')
    def task(self,operation,text):
        snr=self.probe_box.currentText()
        if not snr:self.show_error('먼저 J-Link 찾기를 실행하세요.');return
        self.identity=None
        def run(cancel):
            from cbrain_studio.suite.usb import Lease
            lease=Lease('JLINK-'+snr)
            prog=Programmer(snr,cancel)
            try:return operation(prog), '\n\n'.join(prog.log)
            except Exception as exc:raise RuntimeError(str(exc)+'\n\n'+'\n'.join(prog.log)[-3000:])
            finally:lease.close()
        self.work(text,run,self.probed)
    def probed(self,result):
        identity,log=result;self.identity=identity;device=identity['id'];self.log.setPlainText(log)
        self.identity_label.setText(f'칩 {identity["chip"]}   ·   현재 ID: '+(f'CBRAIN_{device}' if 0<device<0xffffffff else '미부여'))
        if 1<=device<=9999:self.id.setValue(device)
        self.expected.setText(f'이번에 확인한 기기: CBRAIN_{device} — 동글 검색 결과에서 이 번호를 선택하세요.' if 0<device<0xffffffff else '기기 번호를 먼저 부여하세요.')
        self.message('기기 확인·검증 후 재시작했습니다. 동글 연결 탭에서 기기를 검색하세요.');self.update_controls()
    def probe(self):self.task(lambda p:p.probe(),'헤드스테이지 칩과 현재 번호를 읽고 있습니다…')
    def assign(self):
        if not self.identity:return
        device=self.id.value();identity=dict(self.identity)
        entries=Registry().read()['devices'];existing=entries.get(str(device))
        if existing and existing.get('chip') and existing['chip']!=identity['chip']:
            self.show_error('이 번호는 다른 칩에 등록되어 있습니다. 다른 번호를 선택하세요.');return
        if QMessageBox.question(self,'번호 저장',f'현재 헤드스테이지의 번호를 CBRAIN_{device}로 저장할까요?\n기존 UICR 설정을 백업하고 번호를 검증합니다.')!=QMessageBox.Yes:return
        self.task(lambda p:p.assign(device,identity['chip']),'기기 번호를 백업·저장·검증 중입니다. 쓰기 중 취소는 완료 후 반영됩니다…')
    def choose_hex(self):
        path,_=QFileDialog.getOpenFileName(self,'헤드스테이지 HEX 선택',str(base_hex().parent),'Firmware (*.hex)')
        if path:self.hex.setText(path)
    def flash(self):
        if not self.identity:return
        path=self.hex.text();chip=self.identity['chip']
        if QMessageBox.question(self,'펌웨어 설치',f'현재 헤드스테이지에 {Path(path).name}을 설치할까요?\n기기 번호는 보존하고 설치 결과를 검증합니다.')!=QMessageBox.Yes:return
        self.task(lambda p:p.flash(path,chip),'헤드스테이지 펌웨어를 설치·검증 중입니다. 쓰기 중에는 전원을 유지하세요…')
    def connection_changed(self):self.plot.set_units(self.connections.units)
    def update_controls(self):
        if not hasattr(self,'assign_btn'):return
        self.assign_btn.setEnabled(bool(self.identity) and not self.busy and not self.connections.units)
        self.flash_btn.setEnabled(bool(self.identity) and not self.busy and not self.connections.units)
        self.probe_btn.setEnabled(bool(self.probe_box.currentText()) and not self.connections.units)
        ready=bool(self.connections.units and self.connections.units[0].ready and self.connections.units[0].last_time-self.connections.units[0].first_time>=5)
        self.save_btn.setEnabled(ready and not self.busy)
    def save_binding(self):
        if not self.connections.units or not self.connections.units[0].ready:
            self.show_error('실제 센서 수신을 확인한 뒤 연결 조합을 저장하세요.');return
        unit=self.connections.units[0]
        if self.identity and self.identity['id']!=unit.id:self.show_error('확인/부여한 ID와 연결 기기가 다릅니다. 올바른 기기를 선택하세요.');return
        device,serial,label=unit.id,unit.bridge.dongle.serial,unit.label
        bridge_name=unit.bridge.dongle.label
        chip=self.identity['chip'] if self.identity else None
        registry=Registry();replace=False
        entries=registry.read()['devices']
        collisions=[k for k,v in entries.items() if (k==str(device) and v['dongle_serial']!=serial) or (k!=str(device) and v['dongle_serial']==serial)]
        if collisions:
            if QMessageBox.question(self,'연결 조합 교체',f'기존 조합 {", ".join(collisions)}을\nCBRAIN_{device} ↔ {bridge_name}로 교체할까요?')!=QMessageBox.Yes:return
            replace=True
        def save(_):
            # Save only after real link release, so Studio is free to discover it.
            self.connections.release();registry.save(device,serial,label,chip,replace)
        self.work('검증한 조합을 저장하고 동글 연결을 해제 중입니다…',save,lambda _:self.saved(device,bridge_name))
    def saved(self,device,bridge_name):self.connections._released();self.message(f'CBRAIN_{device} ↔ {bridge_name} 준비 완료. Studio에서 검색 후 녹화할 수 있습니다.')

    def open_studio(self):
        def opened(_):
            self.connections._released();launch_studio()
        self.work('Studio로 이동하기 위해 연결을 해제 중입니다…',lambda _:self.connections.release(),opened)
