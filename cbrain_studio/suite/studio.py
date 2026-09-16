from __future__ import annotations
import time
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout,QLabel,QLineEdit,QFileDialog,QMessageBox,QInputDialog,QComboBox,QTableWidgetItem
from cbrain_studio.suite.ui_common import Window,Connections,Plot,plot_area,button,table,open_folder
from cbrain_studio.suite.runtime import Experiment
from cbrain_studio.app.display_buffer import make_display_filter

class Studio(Window):
    def __init__(self):
        super().__init__('CBRAIN Studio','기기를 선택하고 신호를 확인한 뒤 녹화를 시작하세요. 원본 데이터는 표시 설정과 관계없이 저장됩니다.')
        self.experiment=None;self.last_folder=None
        row=QHBoxLayout();self.name=QLineEdit();self.name.setPlaceholderText('실험 이름 (선택 사항)');self.folder=QLineEdit(str(Path.home()/'CBRAIN_recordings'))
        row.addWidget(QLabel('실험'));row.addWidget(self.name,1);row.addWidget(QLabel('저장 위치'));row.addWidget(self.folder,2);self.pick=button('폴더 선택',self.pick_folder);row.addWidget(self.pick);self.layout.addLayout(row)
        self.connections=Connections(self);self.connections.changed.connect(self.changed);self.layout.addWidget(self.connections)
        row=QHBoxLayout();self.start=button('● 녹화 시작',self.record,'record');self.stop=button('■ 녹화 정지',self.stop_record);self.mark=button('이벤트 표시',self.mark_event);self.open=button('저장 폴더 열기',lambda:open_folder(self.last_folder or self.folder.text()))
        for b in [self.start,self.stop,self.mark]:row.addWidget(b)
        row.addStretch();self.elapsed=QLabel('00:00:00');row.addWidget(self.elapsed);row.addWidget(self.open);self.layout.addLayout(row)
        self.health=QLabel('기기 검색부터 시작하세요.');self.health.setObjectName('status');self.layout.addWidget(self.health)
        self.stats=table(['기기','수신 / 파일 기록','수신 샘플','기록 샘플','누락 / 불량']);self.stats.setMinimumHeight(140);self.stats.setMaximumHeight(180);self.layout.addWidget(self.stats)
        row=QHBoxLayout();row.addWidget(QLabel('표시 시간'));self.range=QComboBox()
        for n in [2,5,10,30,60]:self.range.addItem(f'{n}초',n)
        self.range.setCurrentIndex(2);row.addWidget(self.range);row.addWidget(QLabel('진폭'));self.scale=QComboBox()
        for text,value in [('자동',0),('±50 µV',50),('±100 µV',100),('±500 µV',500),('±1000 µV',1000)]:self.scale.addItem(text,value)
        row.addWidget(self.scale);row.addWidget(QLabel('표시 필터'));self.filter=QComboBox();self.filter.addItems(['없음','60 Hz 제거','50 Hz 제거']);row.addWidget(self.filter)
        row.addWidget(QLabel('표시 기기'));self.view=QComboBox();self.view.addItem('전체',None);row.addWidget(self.view);row.addStretch();self.layout.addLayout(row)
        self.plot=Plot();self.layout.addWidget(plot_area(self.plot),1)
        self.range.currentIndexChanged.connect(lambda:self.set_display());self.scale.currentIndexChanged.connect(lambda:self.set_display());self.filter.currentIndexChanged.connect(lambda:self.set_display());self.view.currentIndexChanged.connect(self.select_view)
        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(300);self.update_controls()
    def set_display(self):
        self.plot.seconds=self.range.currentData();self.plot.amplitude=self.scale.currentData()
        self.plot.display_filter=make_display_filter(notch={1:60,2:50}.get(self.filter.currentIndex()))
    def select_view(self):
        selected=self.view.currentData();self.plot.set_units([u for u in self.connections.units if selected is None or u.id==selected])
    def changed(self):
        self.stats.setRowCount(len(self.connections.units));self.view.clear();self.view.addItem('전체',None)
        for i,u in enumerate(self.connections.units):
            self.view.addItem(f'CBRAIN_{u.id}',u.id)
            for c in range(5):self.stats.setItem(i,c,QTableWidgetItem('—'))
        self.select_view()
    def pick_folder(self):
        path=QFileDialog.getExistingDirectory(self,'기록 저장 폴더',self.folder.text())
        if path:self.folder.setText(path)
    def update_controls(self):
        if not hasattr(self,'start'):return
        recording=bool(self.experiment and self.experiment.active)
        self.start.setEnabled(not self.busy and not recording and bool(self.connections.units) and all(u.ready for u in self.connections.units))
        self.stop.setEnabled(recording and not self.busy);self.mark.setEnabled(recording and not self.busy)
        for w in [self.name,self.folder,self.pick]:w.setEnabled(not recording)
        self.connections.locked=recording
    def record(self):
        root,name=self.folder.text().strip(),self.name.text().strip()
        if not root:self.show_error('저장 위치를 선택하세요.');return
        self.work('기기별 기록 파일을 준비하고 있습니다…',lambda _:Experiment(root,name,list(self.connections.units)),self.record_started)
    def record_started(self,experiment):
        self.experiment=experiment;self.last_folder=str(experiment.folder);self.message('녹화 중입니다. 기기별 기록 샘플 수를 확인하세요.')
    def stop_record(self):
        if self.experiment and self.experiment.active:self.work('기록 마무리 및 파일 재열기 검증 중…',lambda _:self.experiment.stop(),self.record_stopped)
    def record_stopped(self,summary):
        good=sum(s['complete'] for s in summary)
        self.message(f'저장 검증 완료 {good}/{len(summary)}개 · {self.last_folder}')
        text='\n'.join(f'CBRAIN_{s["device_id"]}: {s["samples"]:,} samples · '+('저장 확인' if s['complete'] else s['error'] or '빈 파일')+f' · 누락 {s["gaps"]}' for s in summary)
        QMessageBox.information(self,'저장 결과',text+'\n\n'+str(self.last_folder))
    def mark_event(self):
        label,ok=QInputDialog.getText(self,'이벤트 표시','기록할 내용')
        if ok and label:
            try:self.experiment.event(label);self.message('이벤트를 기록했습니다: '+label)
            except Exception as exc:self.show_error(str(exc))
    def tick(self):
        if self.busy:return
        if self.experiment:self.experiment.tick()
        units=self.connections.units;recording=bool(self.experiment and self.experiment.active)
        for i,u in enumerate(units):
            if i>=self.stats.rowCount():continue
            w=u.writer;state=w.error if w and w.error else ('파일 기록 중' if w and w.written else '기록 시작 대기' if w else '미리보기')
            values=[f'CBRAIN_{u.id}',u.status+' / '+state,f'{u.samples:,}',f'{w.written:,}' if w else '—',f'{u.gaps:,} / {u.bad}']
            for col,val in enumerate(values):self.stats.item(i,col).setText(val)
        ready=sum(u.ready for u in units);writers=sum(bool(u.writer and u.writer.written and not u.writer.error) for u in units)
        self.health.setText(f'연결 {ready}/{len(units)}   ·   파일 기록 {writers}/{len(units) if recording else 0}'+('   ·   확인 필요: 신호 또는 저장 상태를 확인하세요.' if units and (ready!=len(units) or any(u.writer and u.writer.error for u in units)) else ''))
        self.health.setStyleSheet('background:#f5dfd9;padding:12px;border-radius:7px;' if any(u.error or u.bad or u.gaps or (u.writer and u.writer.error) or (u.last_time and time.monotonic()-u.last_time>2) for u in units) else '')
        if recording:
            elapsed=int(time.time()-self.experiment.started);self.elapsed.setText(f'{elapsed//3600:02}:{elapsed//60%60:02}:{elapsed%60:02}')
        self.update_controls()
    def _close_event(self,event):
        if self.experiment and self.experiment.active:
            event.ignore()
            if QMessageBox.question(self,'녹화 중','녹화를 마무리하고 종료할까요?')==QMessageBox.Yes:
                self.work('녹화 파일을 마무리하고 있습니다…',lambda _:self.experiment.stop(),lambda _:QTimer.singleShot(100,self.close))
            return
        super()._close_event(event)
