from __future__ import annotations
import json
import time
from dataclasses import asdict
from pathlib import Path
import queue
import numpy as np
from PySide6.QtWidgets import (QWidget,QTabWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QSpinBox,QDoubleSpinBox,QComboBox,QCheckBox,QFileDialog,QGroupBox,QMessageBox)
from cbrain_studio.suite.ui_common import Window,Connections,Plot,plot_area,button
from cbrain_studio.suite.setup import base_hex
from cbrain_studio.suite.runtime import atomic_json
from cbrain_studio.app.fw_config import FwConfig,LedConfig,CB_CONFIG_ADDR
from cbrain_studio.app.hex_stamp import stamp_config,read_region
from cbrain_studio.app.detection import band_power
from cbrain_studio.core import packet
from cbrain_studio.suite import VERSION

class Builder(Window):
    def __init__(self):
        super().__init__('CBRAIN Firmware Builder','측정 조건과 LED 규칙을 설정합니다. 보정이 필요하면 동글로 기기를 연결하세요.')
        self.calibration=None;self.tabs=QTabWidget();self.layout.addWidget(self.tabs)
        config=QWidget();v=QVBoxLayout(config);v.setContentsMargins(20,18,20,18);v.setSpacing(16)
        meas=QGroupBox('측정 설정');g=QVBoxLayout(meas);row=QHBoxLayout();self.sr=QComboBox()
        for n in [256,512,1024,2048]:self.sr.addItem(f'{n} Hz',n)
        self.sr.setCurrentIndex(2);row.addWidget(QLabel('샘플레이트'));row.addWidget(self.sr);self.notch=QComboBox()
        for label,val in [('60 Hz',60),('50 Hz',50),('없음',0)]:self.notch.addItem(label,val)
        row.addWidget(QLabel('기기 Notch'));row.addWidget(self.notch);self.streaming=QCheckBox('신호 전송 / 녹화 사용');self.streaming.setChecked(True);row.addWidget(self.streaming);row.addStretch();g.addLayout(row)
        row=QHBoxLayout();row.addWidget(QLabel('측정 채널'));self.channels=[]
        for i in range(8):box=QCheckBox(str(i));box.setChecked(i<4);self.channels.append(box);row.addWidget(box)
        row.addStretch();g.addLayout(row);v.addWidget(meas)
        leds=QGroupBox('LED 규칙');g=QGridLayout(leds)
        for col,title in enumerate(['사용','채널','대역 시작 Hz','대역 끝 Hz','배수','색상','강도 %','임계값']):g.addWidget(QLabel(title),0,col)
        self.leds=[]
        for i in range(2):
            enabled=QCheckBox(f'LED {i+1}');ch=QSpinBox();ch.setRange(0,7)
            lo=QDoubleSpinBox();lo.setRange(.1,500);lo.setValue(8)
            hi=QDoubleSpinBox();hi.setRange(.2,500);hi.setValue(12)
            mult=QDoubleSpinBox();mult.setRange(.1,100);mult.setValue(3)
            color=QComboBox()
            for label,rgb in [('빨강',(255,0,0)),('초록',(0,255,0)),('파랑',(0,0,255)),('흰색',(255,255,255))]:color.addItem(label,rgb)
            strength=QSpinBox();strength.setRange(0,100);strength.setValue(50)
            threshold=QLabel('보정 전');widgets=[enabled,ch,lo,hi,mult,color,strength,threshold]
            for col,w in enumerate(widgets):g.addWidget(w,i+1,col)
            self.leds.append(widgets)
        v.addWidget(leds)
        row=QHBoxLayout();row.addWidget(QLabel('FFT 시간창'));self.window=QComboBox()
        for n in [125,250,500,1000]:self.window.addItem(f'{n} ms',n)
        self.window.setCurrentIndex(1);row.addWidget(self.window);row.addWidget(QLabel('보정 시간'));self.duration=QSpinBox();self.duration.setRange(5,120);self.duration.setValue(30);self.duration.setSuffix('초');row.addWidget(self.duration);self.calibrate_btn=button('연결 기기로 보정',self.calibrate);row.addWidget(self.calibrate_btn);row.addStretch();v.addLayout(row)
        self.calib_status=QLabel('LED를 사용하지 않으면 보정 없이 공통 녹화 설정을 만들 수 있습니다.');self.calib_status.setWordWrap(True);v.addWidget(self.calib_status)
        row=QHBoxLayout();self.save_btn=button('펌웨어 생성 및 저장',self.generate,'primary');row.addWidget(self.save_btn);row.addStretch();v.addLayout(row)
        text=QLabel('저장한 HEX는 Device Setup의 “펌웨어 설치”에서 업로드하세요. 설정 변경은 저장·설치 후 기기에 적용됩니다.');text.setWordWrap(True);v.addWidget(text);v.addStretch();self.tabs.addTab(config,'측정 · LED 설정')
        preview=QWidget();pv=QVBoxLayout(preview);pv.setContentsMargins(20,18,20,18)
        self.connections=Connections(self,single=True);self.connections.changed.connect(self.connected);pv.addWidget(self.connections)
        self.plot=Plot();pv.addWidget(plot_area(self.plot),1);self.tabs.addTab(preview,'기기 연결 · 신호 확인')
        for w in [self.sr,self.notch,self.window]:w.currentIndexChanged.connect(self.invalidate)
        for box in self.channels:box.toggled.connect(self.invalidate)
        for enabled,ch,lo,hi,mult,color,strength,thr in self.leds:
            enabled.toggled.connect(self.invalidate)
            for w in [ch,lo,hi,mult]:w.valueChanged.connect(self.invalidate)
        self.update_controls()
    def invalidate(self,*_):
        self.calibration=None
        for row in self.leds:row[-1].setText('보정 전')
    def connected(self):self.plot.set_units(self.connections.units);self.invalidate()
    def update_controls(self):
        if hasattr(self,'calibrate_btn'):self.calibrate_btn.setEnabled(not self.busy and bool(self.connections.units and self.connections.units[0].ready))
    def rules(self):
        channels=[i for i,w in enumerate(self.channels) if w.isChecked()]
        if not channels:raise RuntimeError('측정 채널을 하나 이상 선택하세요.')
        rules=[]
        for enabled,ch,lo,hi,mult,color,strength,thr in self.leds:
            if enabled.isChecked() and (ch.value() not in channels or not lo.value()<hi.value()<self.sr.currentData()/2):raise RuntimeError('LED 채널은 측정 채널에 포함되고 대역은 Nyquist 범위 안이어야 합니다.')
            rules.append({'enabled':enabled.isChecked(),'channel':ch.value(),'lo':lo.value(),'hi':hi.value(),'mult':mult.value(),'color':color.currentData(),'intensity':strength.value()})
        return channels,rules
    def calibrate(self):
        if not self.connections.units or not self.connections.units[0].ready:
            self.show_error('실제 센서 수신을 확인한 뒤 보정하세요.');return
        try:channels,rules=self.rules()
        except Exception as exc:self.show_error(str(exc));return
        if not any(r['enabled'] for r in rules):self.show_error('보정할 LED를 선택하세요.');return
        unit=self.connections.units[0];sr=self.sr.currentData();duration=self.duration.value();window=self.window.currentData()
        if sr!=unit.last_block.sr_hz or channels!=list(unit.last_block.order):self.show_error('보정 시 측정 채널·샘플레이트가 현재 수신 설정과 같아야 합니다. 먼저 LED OFF 설정을 설치하세요.');return
        n=32
        while n*2<=sr*window/1000 and n*2<=512:n*=2
        def calibrate(cancel):
            chunks=queue.Queue(256);overflow=threading.Event()
            def consume(b):
                try:chunks.put_nowait(b)
                except queue.Full:overflow.set()
            with unit.lock:unit.listeners.append(consume)
            buffers=[np.empty(0) for _ in rules];values=[[] for _ in rules];end=time.monotonic()+duration
            gaps=unit.gaps;bad=unit.bad
            try:
                while time.monotonic()<end:
                    if cancel.is_set():raise RuntimeError('보정 취소됨')
                    if not unit.ready or unit.gaps!=gaps or unit.bad!=bad or overflow.is_set():raise RuntimeError('보정 중 데이터 누락/끊김. 연결을 확인하고 다시 보정하세요.')
                    try:b=chunks.get(timeout=.2)
                    except queue.Empty:continue
                    for i,r in enumerate(rules):
                        if not r['enabled']:continue
                        buffers[i]=np.concatenate([buffers[i],b.channels[channels.index(r['channel'])]])
                        while len(buffers[i])>=n:
                            values[i].append(band_power(buffers[i][:n],sr,r['lo'],r['hi']));buffers[i]=buffers[i][n:]
                thresholds=[float(np.mean(v))*r['mult'] if v else 0 for v,r in zip(values,rules)]
                if any(r['enabled'] and t<=0 for r,t in zip(rules,thresholds)):raise RuntimeError('유효한 신호에서 임계값을 계산하지 못했습니다.')
                return {'device_id':unit.id,'dongle':unit.bridge.dongle.serial,'thresholds':thresholds,'duration':duration,'time':time.time()}
            finally:
                with unit.lock:unit.listeners.remove(consume)
        import threading
        self.work(f'{duration}초 보정 중입니다. 설정과 기기 연결을 유지하세요…',calibrate,self.calibrated)
    def calibrated(self,result):
        self.calibration=result
        for row,t in zip(self.leds,result['thresholds']):row[-1].setText(f'{t:.2f}')
        self.calib_status.setText(f'CBRAIN_{result["device_id"]} 보정 완료 · 임계값은 이 기기의 원본 신호 기준입니다.');self.message('보정을 완료했습니다. 펌웨어를 저장할 수 있습니다.')
    def generate(self):
        try:
            channels,rules=self.rules()
            if any(r['enabled'] for r in rules) and not self.calibration:raise RuntimeError('활성 LED의 보정을 먼저 완료하세요.')
            if not self.streaming.isChecked() and QMessageBox.question(self,'신호 전송 OFF','이 설정을 설치하면 PC로 신호를 기록할 수 없습니다. 계속할까요?')!=QMessageBox.Yes:return
            thresholds=self.calibration['thresholds'] if self.calibration else [0,0]
            leds=[LedConfig(r['enabled'],r['channel'],self.window.currentData(),r['lo'],r['hi'],t,r['color'],r['intensity']) for r,t in zip(rules,thresholds)]
            config=FwConfig(sr_hz=self.sr.currentData(),notch=self.notch.currentData(),ch_count=len(channels),ch_map=channels,stream_ble=self.streaming.isChecked(),leds=leds)
            output=stamp_config(base_hex().read_text(),config.to_bytes(),CB_CONFIG_ADDR)
            assert FwConfig.from_bytes(read_region(output,CB_CONFIG_ADDR,len(config.to_bytes()))).to_bytes()==config.to_bytes()
            path,_=QFileDialog.getSaveFileName(self,'설정 펌웨어 저장',str(Path.home()/'CBRAIN_custom.hex'),'Firmware (*.hex)')
            if not path:return
            Path(path).write_text(output)
            atomic_json(Path(path).with_suffix('.json'),{'version':VERSION,'config':asdict(config),'calibration':self.calibration})
            self.message(f'펌웨어와 설정 정보를 저장했습니다: {path}')
        except Exception as exc:self.show_error(str(exc))
