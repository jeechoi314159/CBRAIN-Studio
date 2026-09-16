from __future__ import annotations
import threading
import time
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt,QThread,Signal,QTimer,QUrl
from PySide6.QtGui import QColor,QFont,QPainter,QPen,QDesktopServices,QPalette,QTextCursor
from PySide6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QButtonGroup,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QComboBox,QMessageBox,QScrollArea,QPlainTextEdit)
from cbrain_studio.suite import VERSION
from cbrain_studio.suite.usb import Pool,allocate
from cbrain_studio.suite.runtime import Unit,Registry

STYLE='''
QWidget {background:#f0f1f3;color:#252b35;font-size:16px;}
QLabel#title {font-size:28px;font-weight:650;color:#202631;}
QLabel#subtitle {color:#667181;font-size:15px;}
QLabel#status {padding:12px;background:#e4e9ef;border-radius:7px;}
QGroupBox {font-size:17px;font-weight:600;border:1px solid #cbd1d9;border-radius:8px;margin-top:22px;padding:15px 12px 10px;}
QGroupBox::title {subcontrol-origin:margin;left:14px;padding:0 5px;}
QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox,QPlainTextEdit {background:#fff;border:1px solid #bcc5cf;border-radius:5px;padding:7px;min-height:25px;selection-background-color:#3569a3;}
QPushButton {background:#fff;border:1px solid #b9c2cd;border-radius:6px;padding:9px 16px;min-height:25px;font-weight:550;}
QPushButton:hover {background:#e8eef6;border-color:#6590bd;}
QPushButton#primary {background:#32669c;color:white;border-color:#32669c;}
QPushButton#record {background:#a83b3b;color:white;border-color:#a83b3b;}
QPushButton#primary:disabled,QPushButton#record:disabled,QPushButton:disabled {color:#8b939f;background:#e1e4e8;border-color:#d0d5dc;}
QPushButton#deviceChoice {margin:4px 10px;padding:3px 10px;min-height:24px;background:#fff;color:#285b90;border:2px solid #7b94af;}
QPushButton#deviceChoice:checked {background:#32669c;color:#fff;border-color:#24527f;font-weight:650;}
QPushButton#deviceChoice:disabled {background:#e1e4e8;color:#8b939f;border-color:#cbd1d9;}
QTableWidget {background:#fff;alternate-background-color:#f6f7f9;border:1px solid #ccd3dc;gridline-color:#e2e6eb;}
QHeaderView::section {background:#e4e8ee;padding:8px;border:0;border-bottom:1px solid #cbd1d9;font-weight:600;}
QTableWidget::item {padding:5px;} QTableWidget::item:selected {background:#dce8f5;color:#203c58;}
QTabWidget::pane {border:1px solid #ccd2da;border-radius:7px;}
QTabBar::tab {background:#e3e6eb;padding:11px 22px;border-radius:4px;margin-right:4px;}
QTabBar::tab:selected {background:#fff;color:#285b90;}
QCheckBox {spacing:9px;} QCheckBox::indicator {width:20px;height:20px;}
QProgressBar {border:1px solid #cbd1d9;border-radius:5px;text-align:center;min-height:22px;}
QProgressBar::chunk {background:#6b8dab;}
QToolTip {background:#fff;color:#222;border:1px solid #aaa;}
'''

def configure(app):
    app.setStyle('Fusion');app.setFont(QFont('Arial',14));app.setStyleSheet(STYLE)
    pal=app.palette();pal.setColor(QPalette.Window,QColor('#f0f1f3'));pal.setColor(QPalette.Base,QColor('#ffffff'));pal.setColor(QPalette.Text,QColor('#252b35'));pal.setColor(QPalette.WindowText,QColor('#252b35'));app.setPalette(pal)

def button(text,fn,kind=''):
    b=QPushButton(text);b.clicked.connect(fn)
    if kind:b.setObjectName(kind)
    return b

def table(headers):
    t=QTableWidget(0,len(headers));t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide();t.setAlternatingRowColors(True);t.verticalHeader().setDefaultSectionSize(46)
    t.setSelectionBehavior(QAbstractItemView.SelectRows);t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    return t

class LogView(QPlainTextEdit):
    """Follow the last line after log replacement, append, or viewport reflow."""
    def __init__(self):
        super().__init__();self.setReadOnly(True)
        self.textChanged.connect(self.follow_tail)
        self.verticalScrollBar().rangeChanged.connect(lambda _lo,hi:self.verticalScrollBar().setValue(hi))
    def follow_tail(self):
        self.moveCursor(QTextCursor.End);self.ensureCursorVisible()
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

class Job(QThread):
    result=Signal(object);failed=Signal(str)
    def __init__(self,fn):super().__init__();self.fn=fn;self.cancel=threading.Event()
    def run(self):
        try:self.result.emit(self.fn(self.cancel))
        except Exception as exc:self.failed.emit(str(exc))

class Window(QWidget):
    def __init__(self,title,subtitle):
        super().__init__();self.setWindowTitle(f'{title} · {VERSION}');self.job=None;self.busy=False
        self.root=QVBoxLayout(self);self.root.setContentsMargins(24,20,24,20);self.root.setSpacing(14)
        head=QHBoxLayout();label=QLabel(title);label.setObjectName('title');head.addWidget(label);head.addStretch();v=QLabel('CBRAIN  '+VERSION);v.setObjectName('subtitle');head.addWidget(v);self.root.addLayout(head)
        label=QLabel(subtitle);label.setObjectName('subtitle');label.setWordWrap(True);self.root.addWidget(label)
        self.body=QWidget();self.layout=QVBoxLayout(self.body);self.layout.setContentsMargins(0,0,0,0);self.layout.setSpacing(12);self.root.addWidget(self.body,1)
        self.status=QLabel('준비되었습니다.');self.status.setObjectName('status');self.status.setWordWrap(True)
        foot=QHBoxLayout();foot.addWidget(self.status,1);self.cancel_btn=button('취소',self.cancel);self.cancel_btn.hide();foot.addWidget(self.cancel_btn);self.root.addLayout(foot)
        self.quit_btn=button('종료',self.close);self.quit_btn.setMinimumWidth(110)
        self.quit_btn.setToolTip('녹화와 동글 연결을 정리하고 앱을 종료합니다.')
        foot.addWidget(self.quit_btn)
        self.resize(1180,850)
    def message(self,msg):self.status.setText(msg)
    def cancel(self):
        if self.job:self.job.cancel.set();self.message('진행 중인 작업을 안전하게 마무리하는 중입니다…')
    def work(self,text,fn,done=None):
        if self.busy:return
        self.busy=True;self.body.setEnabled(False);self.quit_btn.setEnabled(False);self.cancel_btn.show();self.message(text)
        self.job=Job(fn)
        def success(value):
            try:
                if done:done(value)
                else:self.message('완료되었습니다.')
            except Exception as exc:self.show_error(str(exc))
        self.job.result.connect(success);self.job.failed.connect(self.show_error)
        self.job.finished.connect(self._finished);self.job.start()
    def _finished(self):
        self.busy=False;self.body.setEnabled(True);self.quit_btn.setEnabled(True);self.cancel_btn.hide()
        if hasattr(self,'update_controls'):self.update_controls()
    def show_error(self,msg):self.message('확인 필요 · '+msg)
    def closeEvent(self,event):
        if self.busy:self.cancel();event.ignore();return
        self._close_event(event)
    def _close_event(self,event):
        if hasattr(self,'connections') and self.connections.pool.bridges:
            event.ignore();self.work('동글 연결 해제 중…',lambda _:self.connections.release(),lambda _:QTimer.singleShot(100,self.close));return
        event.accept()


class Connections(QWidget):
    changed=Signal()
    def __init__(self,window,single=False):
        super().__init__();self.window=window;self.single=single;self.pool=Pool();self.units=[];self.registry=Registry();self.locked=False
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        row=QHBoxLayout();self.scan_btn=button('1  기기 검색',self.search,'primary');self.connect_btn=button('2  선택 기기 연결',self.connect_selected,'primary');self.disconnect_btn=button('연결 해제',self.disconnect)
        self.connect_btn.setEnabled(False)
        for b in [self.scan_btn,self.connect_btn,self.disconnect_btn]:row.addWidget(b)
        self.details_btn=button('기기 목록 표시',lambda:self.table.setVisible(not self.table.isVisible()));self.details_btn.hide();row.addWidget(self.details_btn)
        row.addStretch();self.info=QLabel('nRF USB 동글로 검색');row.addWidget(self.info);layout.addLayout(row)
        self.selection_note=QLabel('왼쪽 선택 버튼으로 '+('기기 한 개를 선택하세요. 준비 작업은 한 기기씩 진행합니다.' if single else '녹화할 기기를 선택하세요. 여러 기기를 선택할 수 있습니다.'))
        self.selection_note.setWordWrap(True);layout.addWidget(self.selection_note)
        self.table=table(['선택','헤드스테이지','별칭 / 동물','사용할 동글','상태 / 신호'])
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.cellClicked.connect(self.select_row)
        self.table.setMinimumHeight(145);self.table.setMaximumHeight(200);layout.addWidget(self.table)
        self.choice_group=QButtonGroup(self);self.choice_group.setExclusive(single)
        self.choices={}
        self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(250)
        self.rows={}
    def search(self):
        if self.locked:return
        # Remove old choices before an attempted scan, including failed scans.
        self.display([])
        self.window.work('동글로 주변 기기를 검색 중입니다… 약 8초',lambda cancel:self._search(cancel),self.display)
    def _search(self,cancel):
        self.units=[]
        return self.pool.scan(cancel)
    def display(self,candidates):
        from PySide6.QtWidgets import QLineEdit
        for choice in self.choices.values():self.choice_group.removeButton(choice)
        self.choices={};self.table.setRowCount(0)
        ids=sorted({c.device_id for c in candidates});self.rows={};self.table.setRowCount(len(ids))
        registered=self.registry.read()['devices']
        bridge_names=self.registry.bridge_names(c.dongle.serial for c in candidates)
        for row,device in enumerate(ids):
            observations=[c for c in candidates if c.device_id==device];duplicate=len({c.address for c in observations})>1
            choice=QPushButton('번호 중복' if duplicate else '선택');choice.setObjectName('deviceChoice')
            choice.setCheckable(True);choice.setEnabled(not duplicate);choice.setAccessibleName(f'CBRAIN_{device} 선택')
            self.choice_group.addButton(choice);self.choices[device]=choice;self.table.setCellWidget(row,0,choice)
            choice.toggled.connect(self.selection_changed)
            self.table.setItem(row,1,QTableWidgetItem(f'CBRAIN_{device}'))
            label=QLineEdit(registered.get(str(device),{}).get('label',''));label.setPlaceholderText('선택 사항');self.table.setCellWidget(row,2,label)
            combo=QComboBox();combo.addItem('자동 배정',None)
            for serial in sorted({c.dongle.serial for c in observations},key=lambda s:int(bridge_names[s].rsplit('_',1)[1])):
                combo.addItem(bridge_names[serial],serial)
                port=next(c.dongle.port for c in observations if c.dongle.serial==serial)
                combo.setItemData(combo.count()-1,f'USB 일련번호: {serial}\n포트: {port}',Qt.ToolTipRole)
            combo.currentIndexChanged.connect(lambda _,box=combo:box.setToolTip(box.currentData(Qt.ToolTipRole) or '연결할 동글을 자동으로 배정합니다.'))
            saved=registered.get(str(device),{}).get('dongle_serial');index=combo.findData(saved)
            if index>=0:combo.setCurrentIndex(index)
            self.table.setCellWidget(row,3,combo)
            self.table.setItem(row,4,QTableWidgetItem('번호 중복 — 선택 불가' if duplicate else f'발견 · {max(c.rssi for c in observations)} dBm'))
            self.rows[device]=row
        self.selection_changed()
        if ids:message='왼쪽 선택 버튼을 누른 뒤 연결하세요.'
        elif self.pool.busy and not self.pool.bridges:
            message='검색을 시작하지 못했습니다. 동글을 사용 중인 앱에서 연결 해제 또는 종료 후 다시 검색하세요.'
        elif self.pool.errors and not self.pool.bridges:
            message='동글 검색을 실행하지 못했습니다. 아래 연결 오류를 확인하세요.'
        else:message='검색을 마쳤지만 발견된 기기가 없습니다. 헤드스테이지 전원과 다른 동글의 연결을 확인하세요.'
        self.window.message(message+('  /  '+'; '.join(self.pool.errors) if self.pool.errors else ''))
        self.changed.emit()
    def selected_ids(self):return [d for d,choice in self.choices.items() if choice.isChecked()]
    def select_row(self,row,column):
        if column not in (1,4) or self.locked or self.units or self.window.busy:return
        for device,r in self.rows.items():
            if r==row and self.choices[device].isEnabled():self.choices[device].click();return
    def selection_changed(self):
        ids=self.selected_ids()
        for choice in self.choices.values():
            if choice.isCheckable() and choice.text()!='번호 중복':choice.setText('✓ 선택됨' if choice.isChecked() else '선택')
        if not self.units:
            count=max(len(self.pool.detected),len(self.pool.bridges))
            busy=f' · 사용 중 {len(self.pool.busy)}개' if self.pool.busy else ''
            self.info.setText(f'USB 동글 {count}개{busy} · 기기 {len(self.rows)}개 · 선택 {len(ids)}개')
            if self.pool.busy and not self.rows:
                note='동글 사용 중: '+' / '.join(self.pool.busy.values())+' — 해당 앱에서 연결 해제 또는 종료 후 다시 검색하세요.'
            elif ids:note='선택한 기기: '+', '.join(f'CBRAIN_{d}' for d in ids)+' — 위의 연결 버튼을 누르세요.'
            else:note='왼쪽 선택 버튼으로 '+('기기 한 개를 선택하세요. 준비 작업은 한 기기씩 진행합니다.' if self.single else '녹화할 기기를 선택하세요. 여러 기기를 선택할 수 있습니다.')
            self.selection_note.setText(note)
        self.connect_btn.setText(f'2  CBRAIN_{ids[0]} 연결' if len(ids)==1 else f'2  선택한 {len(ids)}개 연결' if ids else '2  선택 기기 연결')
        self.refresh()
    def connect_selected(self):
        if self.locked or self.units or self.window.busy:return
        ids=self.selected_ids()
        if not ids or (self.single and len(ids)!=1):self.window.show_error('연결할 기기를 '+('하나만' if self.single else '먼저')+' 선택하세요.');return
        forced={d:self.table.cellWidget(self.rows[d],3).currentData() for d in ids}
        labels={d:self.table.cellWidget(self.rows[d],2).text().strip() for d in ids}
        def connect(cancel):
            self.pool.refresh_selected(ids,self.registry.preferred(),cancel)
            rows=[c for c in self.pool.candidates if not forced.get(c.device_id) or c.dongle.serial==forced[c.device_id]]
            chosen=allocate(rows,ids,self.registry.preferred());units=[]
            try:
                for device,c in chosen.items():
                    if cancel.is_set():raise RuntimeError('연결 취소됨')
                    bridge=self.pool.bridges[c.dongle.serial];unit=Unit(bridge,c,labels[device]);units.append(unit);bridge.connect(c)
                self.units=units
                return units
            except Exception:
                self.pool.close();self.units=[];raise
        self.window.work('선택 기기를 재검색하고 동글을 배정 중입니다…',connect,self.connected)
    def connected(self,units):
        for u in units:
            row=self.rows.get(u.id)
            if row is not None:
                combo=self.table.cellWidget(row,3);combo.setCurrentIndex(combo.findData(u.bridge.dongle.serial))
        self.table.setEnabled(False);self.table.hide();self.details_btn.show();self.info.setText(f'선택 {len(units)}개 · 동글 배정 완료')
        self.selection_note.setText('연결 확인 중: '+', '.join(f'CBRAIN_{u.id} ↔ {u.bridge.dongle.label}' for u in units))
        self.window.message('연결 요청 완료 · 실제 기기 ID와 데이터를 확인하는 중입니다.');self.changed.emit()
    def refresh(self):
        for u in self.units:
            if not self.window.busy:
                try:u.bridge.check_events()
                except Exception as exc:u.fail(exc)
            r=self.rows.get(u.id)
            if r is not None:self.table.item(r,4).setText(f'{u.status} · {u.rate:.0f} Hz')
        self.scan_btn.setEnabled(not self.locked and not self.units)
        self.connect_btn.setEnabled(not self.window.busy and not self.locked and not self.units and bool(self.selected_ids()))
        self.disconnect_btn.setEnabled(not self.locked and bool(self.pool.bridges))
        if hasattr(self.window,'update_controls'):self.window.update_controls()
    def disconnect(self):
        if self.locked:return
        self.window.work('동글 연결 해제 중…',lambda _:self.release(),lambda _:self._released())
    def release(self):
        self.units=[]
        self.pool.close()
    def _released(self):
        self.pool.detected=[];self.pool.busy={};self.pool.errors=[]
        for choice in self.choices.values():self.choice_group.removeButton(choice)
        self.choices={};self.table.setEnabled(True);self.table.show();self.details_btn.hide();self.table.setRowCount(0);self.rows={};self.selection_changed();self.info.setText('연결 해제됨');self.changed.emit();self.window.message('연결을 해제했습니다. 다시 검색할 수 있습니다.')


class Plot(QWidget):
    def __init__(self):
        super().__init__();self.units=[];self.seconds=10;self.amplitude=0;self.display_filter=None;self.hidden_ids=set()
        self.setMinimumHeight(230);self.timer=QTimer(self);self.timer.timeout.connect(self.update);self.timer.start(100)
    def set_units(self,units):self.units=units;self.setMinimumHeight(max(230,len(units)*260));self.update()
    def paintEvent(self,event):
        p=QPainter(self);p.fillRect(self.rect(),QColor('#fbfcfd'))
        if not self.units:p.setPen(QColor('#718092'));p.drawText(self.rect(),Qt.AlignCenter,'연결한 기기의 신호가 여기에 표시됩니다.');p.end();return
        width=self.width();height=260
        for i,u in enumerate(self.units):
            top=i*height;p.fillRect(0,top,width,height,QColor('#fcfcfd' if i%2==0 else '#f3f5f7'))
            p.setPen(QColor('#253a50'));p.setFont(QFont('Arial',14,QFont.DemiBold));p.drawText(16,top+28,f'CBRAIN_{u.id}  {u.label}  ·  {u.bridge.dongle.label}  ·  {u.status}')
            if u.id in self.hidden_ids:p.drawText(18,top+115,'파형 숨김 · 기록은 계속됩니다.');continue
            if u.error:
                p.setPen(QColor('#a73528'));p.drawText(18,top+110,'측정 중단 · 헤드스테이지 전원과 증폭기 연결을 확인하세요.');continue
            if u.blocks and u.source=='unverified':
                p.setPen(QColor('#916315'));p.drawText(18,top+110,'센서 확인 기능이 있는 헤드스테이지 펌웨어를 설치한 뒤 다시 연결하세요.');continue
            snap=u.buffer.snapshot(self.seconds,max(20,width-140),u.uv_per_lsb,self.display_filter)
            if snap is None:p.setPen(QColor('#7c8898'));p.drawText(18,top+110,'데이터 수신 대기 중…');continue
            ch=snap['ch'];span=175/max(1,ch);p.setFont(QFont('Arial',10))
            for c in range(ch):
                y=top+55+c*span+span/2;lo=snap['lo'][c];hi=snap['hi'][c]
                amp=self.amplitude or max(10,float(max(np.max(np.abs(lo)),np.max(np.abs(hi)))))
                scale=(span*.4)/amp;left=115;right=width-16
                p.setPen(QPen(QColor('#dce2e9'),1));p.drawLine(left,int(y),right,int(y))
                p.setPen(QColor('#637186'));p.drawText(10,int(y)-3,f'CH {snap["order"][c]}');p.drawText(10,int(y)+12,f'±{amp:.0f} µV')
                xs=np.linspace(left,right,len(lo));p.setPen(QPen(QColor(['#326a9f','#328170','#b97730','#885b9d'][c%4]),1))
                for x,a,b in zip(xs,lo,hi):p.drawLine(int(x),int(np.clip(y-a*scale,y-span/2+2,y+span/2-2)),int(x),int(np.clip(y-b*scale,y-span/2+2,y+span/2-2)))
            p.setPen(QColor('#758292'));p.drawText(115,top+246,f'−{min(self.seconds,u.samples/(u.last_block.sr_hz if u.last_block else 1024)):.1f} s');p.drawText(width-45,top+246,'0 s')
            if snap['led'].shape[0]:
                ls=['ON' if bool(snap['led'][j,-1]) else 'OFF' for j in range(snap['led'].shape[0])]
                p.drawText(250,top+246,'LED  '+ ' / '.join(ls))
        p.end()

def plot_area(plot):
    area=QScrollArea();area.setWidgetResizable(True);area.setWidget(plot);area.setMinimumHeight(230);return area

def open_folder(path):QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))


def launch_studio():
    import subprocess,sys
    if getattr(sys,'frozen',False):
        path=Path(sys.executable).parents[3]/'CBRAIN Studio.app'
        if not path.exists():raise RuntimeError('같은 폴더의 CBRAIN Studio.app을 찾을 수 없습니다.')
        subprocess.Popen(['open',str(path)])
    else:
        path=Path(__file__).resolve().parents[2]/'tools/studio_gui.py'
        subprocess.Popen([sys.executable,str(path)])
