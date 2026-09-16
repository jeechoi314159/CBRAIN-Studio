"""
CBRAIN Studio — multi-headstage data-acquisition GUI.

Design: docs/cbrain_studio_architecture.md. Top→bottom: session controls · integrity
summary · device grid · display settings · MultiScope. Backend: app/session.py
(SessionManager/DeviceLink), app/display_buffer.py.

Principles: save raw / display processed, acquisition separate from recording, no
silent failure (integrity always shown + preflight gate), dropout = alarm-only (no
auto-reconnect), LED = host-reproduced (estimated).

Streaming and recording are independent:
  · Streaming is a global on/off toggle (pairs / disconnects all devices).
  · Recording has its own Start and Stop, and can only run while streaming.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSlider,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from cbrain_studio.app.display_buffer import DisplayBuffer, make_display_filter
from cbrain_studio.app.session import LinkState, SessionManager
from cbrain_studio.hal.bridge import TARGET_ANY, find_bridge_ports
from cbrain_studio.ui.multi_scope import MultiScope

XRANGE = [1, 2, 5, 10, 30, 60]
NOTCH = [("Off", None), ("50 Hz", 50.0), ("60 Hz", 60.0)]
BANDS = [("Wideband", None), ("Delta 0.5–4 Hz", (0.5, 4.0)), ("Theta 4–8 Hz", (4.0, 8.0)),
         ("Alpha 8–12 Hz", (8.0, 12.0)), ("Beta 12–30 Hz", (12.0, 30.0)),
         ("Gamma 30–80 Hz", (30.0, 80.0)), ("Full 0.5–100 Hz", (0.5, 100.0))]
STATE_COLOR = {
    LinkState.IDLE: "#8a9099", LinkState.PAIRING: "#ffd54f",
    LinkState.STREAMING: "#81c784", LinkState.RECORDING: "#e57373",
    LinkState.STALE: "#ffb74d", LinkState.ERROR: "#e53935",
}


class StudioWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CBRAIN Studio — Multi-Acquisition")
        self.session = SessionManager()
        self.buffers: dict[str, DisplayBuffer] = {}     # dongle port → DisplayBuffer
        self._fs_prev: dict[str, tuple] = {}            # dongle port → (t, samples) for effective-fs
        self.streaming = False
        self._alerted_devices: set[str] = set()         # devices already shown the gap/bad guidance
        self._resolved: set[str] = set()                # ports whose device_id is known (for label refresh)
        self._build_ui()
        self.timer = QTimer(self); self.timer.timeout.connect(self._tick); self.timer.start(120)
        self.draw_timer = QTimer(self); self.draw_timer.timeout.connect(self.scope.update); self.draw_timer.start(33)
        self._refresh_ports()
        self._update_buttons()

    # ── UI ──
    def _build_ui(self):
        root = QVBoxLayout(self)

        # (1) session controls
        sc = QHBoxLayout()
        self.sess_name = QLineEdit(); self.sess_name.setPlaceholderText("Session name (optional)")
        self.pre_btn = QPushButton("Preflight"); self.pre_btn.clicked.connect(self._preflight)
        self.stream_btn = QPushButton("▶ Start Streaming"); self.stream_btn.clicked.connect(self._toggle_streaming)
        self.rec_start_btn = QPushButton("● Start Recording"); self.rec_start_btn.clicked.connect(self._start_record)
        self.rec_stop_btn = QPushButton("■ Stop Recording"); self.rec_stop_btn.clicked.connect(self._stop_record)
        self.evt_btn = QPushButton("Event"); self.evt_btn.clicked.connect(self._mark_event)
        self.quit_btn = QPushButton("Quit"); self.quit_btn.clicked.connect(self.close)
        sc.addWidget(QLabel("Session")); sc.addWidget(self.sess_name, 1)
        sc.addWidget(self.pre_btn); sc.addSpacing(12)
        sc.addWidget(self.stream_btn); sc.addSpacing(12)
        sc.addWidget(self.rec_start_btn); sc.addWidget(self.rec_stop_btn); sc.addWidget(self.evt_btn)
        sc.addSpacing(12); sc.addWidget(self.quit_btn)
        root.addLayout(sc)

        # (1b) save location
        loc = QHBoxLayout()
        self.dir_edit = QLineEdit(self.session.out_root)
        dir_btn = QPushButton("Folder…"); dir_btn.clicked.connect(self._pick_dir)
        loc.addWidget(QLabel("Save to")); loc.addWidget(self.dir_edit, 1); loc.addWidget(dir_btn)
        root.addLayout(loc)

        # (2) integrity summary bar
        self.health = QLabel("No devices")
        self.health.setStyleSheet("padding:6px; background:#1b1f24; color:#b0bec5; border-radius:4px;")
        root.addWidget(self.health)

        # (3) add device + status grid
        add = QHBoxLayout()
        self.port_box = QComboBox()
        refresh = QPushButton("↻"); refresh.setFixedWidth(32); refresh.clicked.connect(self._refresh_ports)
        self.num = QSpinBox(); self.num.setRange(0, 9999); self.num.setValue(0)
        self.num.setSpecialValueText("Any (auto-detect)")   # 0 → wildcard: connect to any headstage
        add_btn = QPushButton("＋ Add device"); add_btn.clicked.connect(self._add_device)
        rm_btn = QPushButton("－ Remove selected"); rm_btn.clicked.connect(self._remove_device)
        add.addWidget(QLabel("Dongle")); add.addWidget(self.port_box, 1); add.addWidget(refresh)
        add.addSpacing(10); add.addWidget(QLabel("Headstage #")); add.addWidget(self.num)
        add.addWidget(add_btn); add.addWidget(rm_btn); add.addStretch()
        root.addLayout(add)

        self.grid = QTableWidget(0, 8)
        self.grid.setHorizontalHeaderLabels(["Show", "Headstage", "Dongle", "State",
                                             "Eff. fs", "Gaps/Bad", "Rec. samples", "File"])
        self.grid.verticalHeader().setVisible(False)
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.grid.setMaximumHeight(200)
        self.grid.cellClicked.connect(self._toggle_show)
        root.addWidget(self.grid)

        # (4) display settings
        ds = QHBoxLayout()
        self.xr = QComboBox(); [self.xr.addItem(f"{s} s", s) for s in XRANGE]; self.xr.setCurrentText("10 s")
        self.xr.currentIndexChanged.connect(lambda: self.scope.set_window(self.xr.currentData()))
        self.notch_box = QComboBox(); [self.notch_box.addItem(t, v) for t, v in NOTCH]
        self.notch_box.currentIndexChanged.connect(self._set_filter)
        self.band_box = QComboBox(); [self.band_box.addItem(t, v) for t, v in BANDS]
        self.band_box.currentIndexChanged.connect(self._set_filter)
        self.ph = QSlider(Qt.Horizontal); self.ph.setRange(90, 300); self.ph.setValue(150); self.ph.setFixedWidth(120)
        self.ph.valueChanged.connect(lambda v: self.scope.set_panel_height(v))
        ds.addWidget(QLabel("X-range")); ds.addWidget(self.xr)
        ds.addSpacing(10); ds.addWidget(QLabel("Notch")); ds.addWidget(self.notch_box)
        ds.addSpacing(6); ds.addWidget(QLabel("Display band")); ds.addWidget(self.band_box)
        ds.addWidget(QLabel("⚠ Display only — saved data is raw")); ds.addStretch()
        ds.addWidget(QLabel("Panel height")); ds.addWidget(self.ph)
        root.addLayout(ds)

        # (5) MultiScope (scroll)
        self.scope = MultiScope()
        area = QScrollArea(); area.setWidget(self.scope); area.setWidgetResizable(True)
        root.addWidget(area, 1)

        self.resize(1120, 820)

    # ── button enable/label state ──
    def _update_buttons(self):
        rec = self.session.recording
        self.stream_btn.setText("⏸ Stop Streaming" if self.streaming else "▶ Start Streaming")
        self.pre_btn.setEnabled(self.streaming)
        self.rec_start_btn.setEnabled(self.streaming and not rec)
        self.rec_stop_btn.setEnabled(rec)
        self.evt_btn.setEnabled(rec)

    # ── dongle/device ──
    def _refresh_ports(self):
        used = {lk.port for lk in self.session.links}
        self.port_box.clear()
        free = [p for p in find_bridge_ports() if p not in used]
        self.port_box.addItems(free or ["(no dongle)"])

    def _add_device(self):
        ports = [p for p in find_bridge_ports() if p not in {lk.port for lk in self.session.links}]
        if not ports:
            QMessageBox.warning(self, "No dongle", "No available dongle. Check USB, then ↻."); return
        port = self.port_box.currentText()
        if port not in ports:
            port = ports[0]
        target = self.num.value() or TARGET_ANY   # 0 → wildcard (auto-detect)
        if target != TARGET_ANY and any(lk.target_id == target for lk in self.session.links):
            QMessageBox.warning(self, "Duplicate", f"CBRAIN_{target} is already added."); return
        db = DisplayBuffer(sr_hz=1024, max_seconds=max(XRANGE))
        self.buffers[port] = db
        link = self.session.add(port, target, bus_consumer=db.push)
        if self.streaming:                       # pair immediately only if already streaming
            try:
                link.pair()
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "Pairing failed", str(e))
                self.session.remove(link); self.buffers.pop(port, None); return
        if target != TARGET_ANY:
            self.num.setValue(target + 1)        # convenience for sequential numbering
        self._rebuild_grid(); self._refresh_ports(); self._sync_scope()

    def _pick_dir(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "Choose save folder", self.dir_edit.text() or os.path.expanduser("~"))
        if d:
            self.dir_edit.setText(d)
            self.session.out_root = d

    def _toggle_streaming(self):
        if not self.streaming:
            # start: pair all idle links
            self._alerted_devices.clear()        # fresh session → guide again if problems recur
            failed = []
            for lk in self.session.links:
                if lk.state == LinkState.IDLE:
                    try:
                        lk.pair()
                    except Exception as e:  # noqa: BLE001
                        failed.append(f"CBRAIN_{lk.target_id}: {e}")
            self.streaming = True
            if failed:
                QMessageBox.warning(self, "Some devices failed to pair", "\n".join(failed))
        else:
            # stop: stop recording first, then disconnect all (keep them in the grid)
            if self.session.recording:
                self.session.stop_recording()
            self.session.close_all()
            self.streaming = False
        self._update_buttons(); self._sync_scope()

    def _remove_device(self):
        r = self.grid.currentRow()
        if r < 0 or r >= len(self.session.links):
            return
        link = self.session.links[r]
        self.buffers.pop(link.port, None)
        self.session.remove(link)
        self._rebuild_grid(); self._refresh_ports(); self._sync_scope()

    def _device_label(self, lk):
        """discovered device_id 우선 표시. 와일드카드는 연결 전까지 미확정."""
        if lk.device_id is not None:
            return f"CBRAIN_{lk.device_id}"
        return "CBRAIN_? (detecting…)" if lk.is_wildcard else f"CBRAIN_{lk.target_id}"

    def _toggle_show(self, row, col):
        if col == 0 and 0 <= row < len(self.session.links):
            it = self.grid.item(row, 0)
            it.setText("☐" if it.text() == "☑" else "☑")
            self._sync_scope()

    def _rebuild_grid(self):
        self.grid.setRowCount(len(self.session.links))
        for i, lk in enumerate(self.session.links):
            self.grid.setItem(i, 0, QTableWidgetItem("☑"))
            self.grid.setItem(i, 1, QTableWidgetItem(self._device_label(lk)))
            self.grid.setItem(i, 2, QTableWidgetItem(os.path.basename(lk.port)))
            for c in range(3, 8):
                self.grid.setItem(i, c, QTableWidgetItem("—"))

    def _sync_scope(self):
        panels = []
        for i, lk in enumerate(self.session.links):
            it = self.grid.item(i, 0)
            if it and it.text() == "☑":
                panels.append((self._device_label(lk), self.buffers[lk.port],
                               self._panel_state(lk)))
        self.scope.set_panels(panels)

    def _panel_state(self, lk):
        def fn():
            s = lk.stats
            tag = {"recording": "●REC", "streaming": "STREAM", "pairing": "pairing…",
                   "stale": "▲STALE", "idle": "IDLE", "error": "ERR"}.get(lk.state.value, "")
            return f"[{tag}] {s.sr_hz}Hz {s.channel_count}ch  gaps {s.recent_gaps}"
        return fn

    # ── display filter (notch + band, composed) ──
    def _set_filter(self):
        band = self.band_box.currentData()
        notch = self.notch_box.currentData()
        f = make_display_filter(band=band, notch=notch)
        if (band or notch) and f is None:
            QMessageBox.information(self, "Filter unavailable",
                                    "scipy is not installed, so display filters can't run.\n"
                                    "Run `pip install scipy` to use them. (Saved data is unaffected.)")
            self.notch_box.setCurrentIndex(0); self.band_box.setCurrentIndex(0); return
        self.scope.set_filter(f)

    # ── recording ──
    def _preflight(self):
        rep = self.session.preflight()
        if rep.ok:
            QMessageBox.information(self, "Preflight passed", "All checks OK — you can start recording.")
        else:
            QMessageBox.warning(self, "Preflight failed", "Fix the following:\n\n• " + "\n• ".join(rep.failures))

    def _start_record(self):
        if self.session.recording:
            return
        self.session.out_root = self.dir_edit.text() or self.session.out_root
        rep = self.session.start_recording(meta={"name": self.sess_name.text()})
        if not rep.ok:
            QMessageBox.warning(self, "Cannot start recording (preflight failed)",
                                "Fix the following:\n\n• " + "\n• ".join(rep.failures))
            return
        self._update_buttons()

    def _stop_record(self):
        if not self.session.recording:
            return
        summary = self.session.stop_recording()
        n = len(summary["devices"])
        QMessageBox.information(self, "Recording stopped",
                               f"Saved {n} device(s)\n{summary['session_dir']}")
        self._update_buttons()

    def _mark_event(self):
        label, ok = QInputDialog.getText(self, "Event marker", "Label:")
        if ok and label:
            self.session.mark_event(label)

    # ── periodic refresh ──
    def _tick(self):
        self.session.tick()
        streaming = recording = mismatch = 0
        gaps = bad = 0
        problems = []                            # (name, gaps, bad) for the guidance popup
        for i, lk in enumerate(self.session.links):
            s = lk.stats
            if lk.state in (LinkState.STREAMING, LinkState.RECORDING):
                streaming += 1
            if lk.state == LinkState.RECORDING:
                recording += 1
            if lk.id_mismatch():
                mismatch += 1
            gaps += s.recent_gaps; bad += s.frames_bad
            if s.recent_gaps or s.frames_bad:
                problems.append((self._device_label(lk), s.recent_gaps, s.frames_bad))
            # grid update
            self._set(i, 3, lk.state.value, STATE_COLOR.get(lk.state, "#b0bec5"))
            self._set(i, 4, f"{self._eff_fs(lk):.1f}" if s.blocks else "—")
            self._set(i, 5, f"{s.recent_gaps}/{s.frames_bad}"
                      + (f"  (init {s.gaps})" if s.gaps > s.recent_gaps else ""),
                      "#e57373" if (s.recent_gaps or s.frames_bad) else None)
            self._set(i, 6, str(s.written_samples))
            self._set(i, 7, os.path.basename(lk.rec.path) if lk.rec and lk.rec.path else "—")
            if lk.id_mismatch():
                self._set(i, 1, f"target {lk.target_id} ≠ got {lk.device_id}", "#e53935")
            else:
                self._set(i, 1, self._device_label(lk))     # wildcard resolves to real id here

        n = len(self.session.links)
        if n == 0:
            self.health.setText("No devices — add a dongle"); self.health.setStyleSheet(
                "padding:6px; background:#1b1f24; color:#b0bec5; border-radius:4px;")
        elif not self.streaming:
            self.health.setText(f"Streaming stopped — {n} device(s) registered")
            self.health.setStyleSheet("padding:6px; background:#1b1f24; color:#b0bec5; border-radius:4px;")
        else:
            ok = (streaming == n and mismatch == 0 and gaps == 0 and bad == 0
                  and (not self.session.recording or recording == n))
            disk = self.session._disk_free(self.session.out_root)
            disk_s = f"{disk/1e9:.0f}GB" if disk else "?"
            rec_s = f"  ●REC {recording}/{n}" if self.session.recording else ""
            msg = f"● Streaming {streaming}/{n}{rec_s}   gaps {gaps}  bad {bad}   disk {disk_s}"
            if mismatch:
                msg += f"   ⚠ number mismatch {mismatch}"
            self.health.setText(msg)
            self.health.setStyleSheet(
                f"padding:6px; border-radius:4px; color:#0a0a0a; font-weight:bold; "
                f"background:{'#66bb6a' if ok else '#ef5350'};")

        # when a wildcard link discovers its device_id, refresh the scope panel labels
        resolved = {lk.port for lk in self.session.links if lk.device_id is not None}
        if resolved != self._resolved:
            self._resolved = resolved
            self._sync_scope()

        # first time a device shows gaps/bad this session → pop up how to fix it
        new = [p for p in problems if p[0] not in self._alerted_devices]
        if self.streaming and new:
            self._alerted_devices.update(p[0] for p in problems)
            QTimer.singleShot(0, lambda p=list(problems): self._show_integrity_guidance(p))

    def _show_integrity_guidance(self, problems):
        rows = "\n".join(f"   • {name}:  {g} gap(s),  {b} bad frame(s)" for name, g, b in problems)
        QMessageBox.warning(
            self, "Data integrity — gaps / bad frames",
            f"{rows}\n\n"
            "What this means\n"
            "  • Gaps — data blocks were lost for that span. They are logged in the\n"
            "    recording as discontinuity markers; the surrounding data is intact.\n"
            "  • Bad — frames rejected by the CRC check (corrupted in transit).\n\n"
            "How to fix — try in this order\n"
            "  1. RF range / interference: move the headstage closer to the dongle.\n"
            "     Keep the dongle away from USB-3 ports and hubs (they radiate 2.4 GHz\n"
            "     noise) — a short USB extension cable to move it off the Mac helps a lot.\n"
            "  2. Mac hijacking the headstage: quit any app using Mac Bluetooth (e.g.\n"
            "     the Firmware Builder). The dongle streams over USB only.\n"
            "  3. If it persists: power-cycle the headstage; it re-pairs in ~15–20 s.\n\n"
            "This appears once per device — the grid and the status bar keep showing\n"
            "live gap/bad counts in red.")

    def _eff_fs(self, lk):
        """Effective sample rate over the last ~2 s window (reveals data loss)."""
        import time
        s = lk.stats
        now = time.monotonic()
        prev = self._fs_prev.get(lk.port)
        self._fs_prev[lk.port] = (now, s.samples_total)
        if prev is None:
            return 0.0
        dt = now - prev[0]
        return (s.samples_total - prev[1]) / dt if dt > 0.2 else lk.stats.sr_hz

    def _set(self, row, col, text, color=None):
        it = self.grid.item(row, col)
        if it is None:
            it = QTableWidgetItem(); self.grid.setItem(row, col, it)
        it.setText(str(text))
        if color:
            it.setForeground(QColor(color))

    def closeEvent(self, e):
        if self.session.recording:
            if QMessageBox.question(self, "Recording", "Recording is in progress. Stop and quit?") != QMessageBox.Yes:
                e.ignore(); return
            self.session.stop_recording()
        self.session.close_all()
        e.accept(); QApplication.quit()


def run() -> int:
    import sys
    app = QApplication(sys.argv)
    w = StudioWindow(); w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
