"""
CBRAIN Pairer — 학생용 원클릭 페어링 + 스트리밍 앱.

전제: 헤드스테이지는 CBRAIN ID Assigner 로 CBRAIN_N 지정됨, 동글은 naive(범용 cb_bridge 펌웨어를
스태프가 한 번만 DFU 로 구움). 학생은 이 앱에서:
  1) 동글 자동 탐지 → 2) 헤드스테이지 번호 선택 → 3) Pair(누르면 CDC 로 SET_TARGET 전송 →
  동글이 flash 에 저장 + CBRAIN_N 에만 배타 연결) → 4) 라이브 스코프 + 녹화.
DFU/부트로더/J-Link 불필요. Mac 블루투스 무관(동글 CDC=pyserial).
"""
from __future__ import annotations

import asyncio
import os
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from cbrain_studio.app.acquisition import AcquisitionManager
from cbrain_studio.app.recording import DeviceRecorder
from cbrain_studio.hal.bridge import BridgeTransport, find_bridge_ports, set_target_frame
from cbrain_studio.ui.scope import Scope


def _run(coro):
    """BridgeTransport 의 async 메서드(실제 await 없음)를 동기로 실행."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Pairer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CBRAIN Pairer")
        self.tx: BridgeTransport | None = None
        self.acq: AcquisitionManager | None = None
        self.rec: DeviceRecorder | None = None
        self._rec_lock = threading.Lock()
        self._want_rec = False
        self._target: int | None = None       # 현재 페어링(표시/녹화 필터) 대상
        self._match_blocks = 0                 # 대상 번호로 받은 블록 수
        self._last_blocks = 0
        self._stale = 0
        self.out_dir = os.path.join(os.path.expanduser("~"), "CBRAIN_recordings")

        self.port_box = QComboBox()
        refresh = QPushButton("↻"); refresh.setFixedWidth(34); refresh.clicked.connect(self._refresh_ports)
        self.num = QSpinBox(); self.num.setRange(1, 9999); self.num.setValue(1)
        self.num.valueChanged.connect(self._update_target_lbl)
        self.target_lbl = QLabel()
        self.pair_btn = QPushButton("Pair && Stream"); self.pair_btn.clicked.connect(self._pair)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)

        top = QHBoxLayout()
        top.addWidget(QLabel("Dongle")); top.addWidget(self.port_box, 1); top.addWidget(refresh)
        top.addSpacing(16)
        top.addWidget(QLabel("Headstage #")); top.addWidget(self.num); top.addWidget(self.target_lbl)
        top.addStretch()
        top.addWidget(self.pair_btn); top.addWidget(self.stop_btn)

        self.status = QLabel("동글을 꽂고 → 헤드스테이지 번호 선택 → Pair.")
        self.status.setStyleSheet("color:#8a9099;")
        self.rec_btn = QPushButton("● Record"); self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._toggle_rec); self.rec_btn.setEnabled(False)
        self.rec_lbl = QLabel(""); self.rec_lbl.setStyleSheet("color:#8a9099;")
        self.quit_btn = QPushButton("Quit"); self.quit_btn.clicked.connect(self.close)
        srow = QHBoxLayout()
        srow.addWidget(self.rec_btn); srow.addWidget(self.rec_lbl, 1); srow.addWidget(self.quit_btn)

        self.scope = Scope(4)

        root = QVBoxLayout(self)
        root.addLayout(top); root.addWidget(self.status); root.addLayout(srow)
        root.addWidget(QLabel("Live (per-channel)")); root.addWidget(self.scope, 1)
        self.resize(920, 580)

        self._refresh_ports(); self._update_target_lbl()
        self.timer = QTimer(self); self.timer.timeout.connect(self._tick); self.timer.start(150)

    # ── 동글/타깃 ──
    def _refresh_ports(self):
        self.port_box.clear()
        ports = find_bridge_ports()
        if ports:
            self.port_box.addItems(ports)
            self.port_box.setCurrentIndex(len(ports) - 1)   # 데이터 포트는 높은 번호 쪽
        else:
            self.port_box.addItem("(동글 없음)")

    def _update_target_lbl(self):
        self.target_lbl.setText(f"→ <b>CBRAIN_{self.num.value()}</b>")

    # ── 페어링 + 스트리밍 ──
    def _pair(self):
        ports = find_bridge_ports()
        if not ports:
            self.status.setText("❌ 동글이 안 보입니다 — USB 확인 후 ↻"); return
        target = self.num.value()

        if self.tx is None:                          # 최초: 포트 열기 + 수집 시작
            port = self.port_box.currentText()
            if port not in ports:
                port = ports[-1]
            self.tx = BridgeTransport(port)
            self.acq = AcquisitionManager(self.tx)
            self.acq.start()
            self.acq.bus.subscribe(self._on_block)   # scope 표시는 _on_block 에서 타깃 필터 후
            _run(self.tx.open())

        # (재)페어링: 지정 번호로 SET_TARGET + 표시 초기화
        self._target = target
        self._match_blocks = 0; self._last_blocks = -1; self._stale = 0
        self.scope.set_channels(self.scope.ch)       # 이전 파형 지움
        _run(self.tx.send(set_target_frame(target)))
        self.status.setText(f"CBRAIN_{target} 페어링 전송 — 연결 대기…")
        self.stop_btn.setEnabled(True); self.rec_btn.setEnabled(True)
        # pair_btn 은 계속 활성 → 번호 바꿔 다시 Pair 하면 재페어링

    def _on_block(self, blk):
        # reader 스레드 컨텍스트. **지정한 번호만** 표시/녹화 — 다른 device_id 는 완전히 무시.
        if self._target is None or blk.device_id != self._target:
            return
        if blk.channel_count != self.scope.ch:
            self.scope.set_channels(blk.channel_count)
        self.scope.push(blk)
        self._match_blocks += 1
        with self._rec_lock:
            if self._want_rec and self.rec is None:
                os.makedirs(self.out_dir, exist_ok=True)
                self.rec = DeviceRecorder(self.out_dir, blk.device_id)
                self.rec.open(blk.channel_count, blk.sr_hz, blk.order)
            if self.rec is not None:
                tb = self.acq.timebase(blk.device_id)
                counters = list(range(blk.first_counter, blk.first_counter + blk.n_samples))
                ts = tb.sample_times(blk.first_counter, blk.n_samples)
                self.rec.write_block(blk, counters, ts)

    def _tick(self):
        self.scope.update()
        if self.tx is None or self._target is None:
            return
        b = self._match_blocks
        if b != self._last_blocks:
            self._last_blocks = b; self._stale = 0
            msg = f"✅ CBRAIN_{self._target} 스트리밍 · blocks={b}"
            with self._rec_lock:
                if self.rec is not None:
                    msg += f" · 녹화 {self.rec.n_written} samples"
            self.status.setText(msg)
        else:
            self._stale += 1
            if self._stale == 12:                    # ~1.8s 지정 번호 신호 없음
                self.status.setText(f"… CBRAIN_{self._target} 신호 없음 "
                                    f"(그 번호 헤드스테이지가 없거나 미페어링)")

    def _toggle_rec(self, on):
        if on:
            if self._match_blocks == 0:
                self.rec_btn.setChecked(False)
                self.status.setText("아직 지정 번호 데이터 없음 — 연결 후 녹화")
                return
            self._want_rec = True
            self.rec_lbl.setText("● 녹화 시작…")
        else:
            self._want_rec = False
            with self._rec_lock:
                rec, self.rec = self.rec, None
            if rec is not None:
                path = rec.close()
                self.rec_lbl.setText(f"저장됨: {path}")

    def _stop(self):
        self._toggle_rec(False); self.rec_btn.setChecked(False)
        if self.acq is not None:
            self.acq.stop()
        if self.tx is not None:
            _run(self.tx.close())
        self.tx = self.acq = None
        self._target = None; self._match_blocks = 0
        self.scope.set_channels(self.scope.ch)   # 파형 지움
        self.stop_btn.setEnabled(False); self.rec_btn.setEnabled(False)
        self.status.setText("정지됨.")

    def closeEvent(self, e):
        try:
            self._stop()
        finally:
            e.accept(); QApplication.quit()


def run() -> int:
    import sys
    app = QApplication(sys.argv)
    w = Pairer(); w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
