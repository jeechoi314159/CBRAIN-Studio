"""
CBRAIN Firmware Builder — 사용자 의도대로 펌웨어(<name>.hex)를 만드는 GUI.

측정 조건 + 2개 LED 뇌파-대역 폐루프 규칙을 정하면, 설정 blob 을 base.hex 에 찍어
이름 붙인 펌웨어를 저장한다(툴체인 불필요). GUI 측정 절대 임계(연결 시 캘리브레이션),
색/강도 즉시 미리보기 + live LED. docs/firmware_configurator.md.

블럭: 측정조건 · LED0 · LED1 · 디바이스(캘리브/live) · 스펙트럼 미리보기 · 빌드/저장.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import numpy as np
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider,
    QSpinBox, QVBoxLayout, QWidget,
)

from cbrain_studio.app.detection import band_power
from cbrain_studio.app.fw_config import CB_CONFIG_ADDR, FwConfig, LedConfig
from cbrain_studio.app.hex_stamp import stamp_config
from cbrain_studio.app.led_gate import BandPowerGate
from cbrain_studio.core import packet
from cbrain_studio.hal.bridge import BridgeTransport, find_bridge_ports, set_target_frame
from cbrain_studio.ui.builder_scope import BuilderScope

N_INPUTS = 8            # IN0–IN7


def fft_n(sr: int, window_ms: int) -> int:
    """윈도우 샘플수를 2의 거듭제곱(32..512)으로 내림 — 펌웨어 cb_dsp_pow2_floor 와 일치."""
    w = int(sr * window_ms / 1000)
    n = 32
    while n * 2 <= w and n * 2 <= 512:
        n *= 2
    return n

SR_CHOICES = [256, 512, 1024, 2048]
NOTCH_CHOICES = [("off", 0), ("60 Hz", 60), ("50 Hz", 50)]
DRAW_MS = 33


def _last_dir(fallback: str) -> str:
    """파일 대화상자를 마지막에 열었던 폴더에서 열기 (앱 재시작에도 유지)."""
    d = QSettings("CBRAIN", "FirmwareBuilder").value("last_dir", "") or fallback
    return d if os.path.isdir(d) else (fallback if os.path.isdir(fallback) else os.path.expanduser("~"))


def _remember_dir(path: str) -> None:
    if path:
        QSettings("CBRAIN", "FirmwareBuilder").setValue("last_dir", os.path.dirname(path))


def _find_base_hex() -> str:
    """base.hex 탐색: 앱 번들 안 → 앱 폴더(app/firmware) → 저장소 빌드 산출물 순.
    .app 으로 실행할 때 저장소 경로가 없으므로 여러 후보를 본다."""
    import sys
    cands: list[str] = []
    if getattr(sys, "frozen", False):
        mei = getattr(sys, "_MEIPASS", "")
        if mei:
            cands.append(os.path.join(mei, "base.hex"))              # 번들에 포함시킨 것
            appdir = os.path.dirname(os.path.dirname(os.path.dirname(mei)))   # …/app
            cands.append(os.path.join(appdir, "firmware", "headstage_common.hex"))
            cands.append(os.path.join(os.path.dirname(appdir),
                                      "firmware/cb_intan/build/cb_intan/zephyr/zephyr.hex"))
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cands.append(os.path.join(repo, "app/firmware/headstage_common.hex"))
    cands.append(os.path.join(repo, "firmware/cb_intan/build/cb_intan/zephyr/zephyr.hex"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return cands[-1]        # 없으면 마지막 후보(오류 메시지에 경로가 보이도록)


class LedPreview(QWidget):
    """색×강도를 즉시 보여주는 스와치."""
    def __init__(self):
        super().__init__()
        self.setFixedSize(64, 64)
        self.rgb = (255, 0, 0)
        self.intensity = 100
        self.on = True

    def set(self, rgb, intensity, on=True):
        self.rgb, self.intensity, self.on = rgb, intensity, on
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = (self.intensity / 100.0) if self.on else 0.0
        col = QColor(int(self.rgb[0] * f), int(self.rgb[1] * f), int(self.rgb[2] * f))
        p.setBrush(col); p.setPen(QColor("#3a4048"))
        p.drawRoundedRect(2, 2, self.width() - 4, self.height() - 4, 10, 10)
        p.end()


class LedPanel(QGroupBox):
    """LED 한 개: 채널·대역 → 캘리브레이션(자동 임계) → 색/강도 + 즉시 미리보기."""
    def __init__(self, title: str, default_color, on_live=None):
        super().__init__(title)
        self._on_live = on_live            # (rgb, intensity) → live device callback
        self.enabled = QCheckBox("Enabled"); self.enabled.setChecked(True)
        self.channel = QSpinBox(); self.channel.setRange(0, 15)
        self.lo = QDoubleSpinBox(); self.lo.setRange(0.1, 500); self.lo.setValue(8.0)
        self.hi = QDoubleSpinBox(); self.hi.setRange(0.2, 500); self.hi.setValue(12.0)
        self.win = QSpinBox(); self.win.setRange(100, 1000); self.win.setSingleStep(50); self.win.setValue(250); self.win.setSuffix("ms")
        # 캘리브레이션: baseline·mult 는 사용자 설정, threshold 는 자동 계산(직접입력 X)
        self.baseline = QSpinBox(); self.baseline.setRange(5, 600); self.baseline.setValue(30); self.baseline.setSuffix("s")
        self.mult = QDoubleSpinBox(); self.mult.setRange(1.0, 20); self.mult.setValue(3.0); self.mult.setSingleStep(0.5); self.mult.setPrefix("×")
        self.thr = QDoubleSpinBox(); self.thr.setRange(0, 1e12); self.thr.setDecimals(0)
        self.thr.setReadOnly(True); self.thr.setButtonSymbols(QDoubleSpinBox.NoButtons)  # 자동 계산 표시
        self.thr.setStyleSheet("color:#81c784;")
        self.r = self._color_slider(default_color[0]); self.g = self._color_slider(default_color[1]); self.b = self._color_slider(default_color[2])
        self.inten = QSlider(Qt.Horizontal); self.inten.setRange(0, 100); self.inten.setValue(100); self.inten.setFixedWidth(90)
        self.preview = LedPreview(); self.preview.rgb = default_color

        row = QHBoxLayout(self)
        form = QVBoxLayout()
        def line(label, *ws):
            h = QHBoxLayout(); h.addWidget(QLabel(label))
            for w in ws: h.addWidget(w)
            h.addStretch(); form.addLayout(h)
        line("Channel", self.channel, QLabel("Band"), self.lo, QLabel("–"), self.hi, QLabel("Hz"), self.win)
        line("baseline", self.baseline, QLabel("mult"), self.mult, QLabel("→ threshold"), self.thr)
        line("R", self.r, QLabel("G"), self.g, QLabel("B"), self.b, QLabel("Intensity"), self.inten)
        form.addWidget(self.enabled)
        row.addLayout(form, 1)
        pv = QVBoxLayout(); pv.addWidget(QLabel("Preview"), 0, Qt.AlignHCenter); pv.addWidget(self.preview); pv.addStretch()
        row.addLayout(pv)
        for s in (self.r, self.g, self.b, self.inten):
            s.valueChanged.connect(self._update_preview)
        self.enabled.toggled.connect(self._update_preview)
        self._update_preview()

    def _color_slider(self, val):
        s = QSlider(Qt.Horizontal); s.setRange(0, 255); s.setValue(val); s.setFixedWidth(90)
        return s

    def _rgb(self): return (self.r.value(), self.g.value(), self.b.value())

    def _update_preview(self):
        rgb, pct, on = self._rgb(), self.inten.value(), self.enabled.isChecked()
        self.preview.set(rgb, pct, on)
        if self._on_live and on:
            self._on_live(rgb, pct)

    def from_config(self, c: LedConfig) -> None:
        """기존 펌웨어에서 읽은 설정으로 UI 채우기."""
        self.enabled.setChecked(bool(c.enabled))
        self.channel.setValue(int(c.channel))
        self.lo.setValue(float(c.band_lo)); self.hi.setValue(float(c.band_hi))
        self.win.setValue(int(c.window_ms))
        self.thr.setValue(float(c.threshold))
        r, g, b = c.color
        self.r.setValue(int(r)); self.g.setValue(int(g)); self.b.setValue(int(b))
        self.inten.setValue(int(c.intensity))

    def to_config(self) -> LedConfig:
        return LedConfig(
            enabled=self.enabled.isChecked(), channel=self.channel.value(),
            window_ms=self.win.value(), band_lo=self.lo.value(), band_hi=self.hi.value(),
            threshold=self.thr.value(), color=self._rgb(), intensity=self.inten.value())

    def load_config(self, c: LedConfig):
        self.enabled.setChecked(c.enabled); self.channel.setValue(c.channel)
        self.win.setValue(c.window_ms); self.lo.setValue(c.band_lo); self.hi.setValue(c.band_hi)
        self.thr.setValue(c.threshold)
        self.r.setValue(c.color[0]); self.g.setValue(c.color[1]); self.b.setValue(c.color[2])
        self.inten.setValue(c.intensity)


class FwBuilderWindow(QWidget):
    def __init__(self, base_hex: str | None = None):
        super().__init__()
        self.setWindowTitle("CBRAIN Firmware Builder")
        self.base_hex = base_hex or _find_base_hex()

        # BLE (캘리브/live) 상태
        self._ble_state = "idle"
        self._ble_err = ""
        self._calibs = []           # 진행 중 캘리브레이션 [{idx,gate,end,mult,row}, …]
        self._channels = [0, 1, 2, 3]   # 레코딩 채널(IN 인덱스)
        self._built = None          # (hex_text, blob) — '생성' 후 '저장' 대기

        self.scope = BuilderScope(len(self._channels), 1024)
        self._build_ui()

        self.draw_timer = QTimer(self); self.draw_timer.timeout.connect(self._tick)
        self.draw_timer.start(DRAW_MS)          # 미리보기 상시 렌더

    # ── UI ──
    def _build_ui(self):
        root = QVBoxLayout(self)
        # 측정 조건
        meas = QGroupBox("Measurement"); ml = QHBoxLayout(meas)
        self.sr = QComboBox(); self.sr.addItems([f"{s} Hz" for s in SR_CHOICES]); self.sr.setCurrentText("1024 Hz")
        self.notch = QComboBox(); [self.notch.addItem(t, v) for t, v in NOTCH_CHOICES]; self.notch.setCurrentIndex(1)
        self.ch_btn = QPushButton("Select channels…"); self.ch_btn.clicked.connect(self._select_channels)
        self.ch_lbl = QLabel(); self.ch_lbl.setStyleSheet("color:#8a9099;")
        self.stream = QCheckBox("BLE streaming"); self.stream.setChecked(True)
        for lbl, w in (("Sample rate", self.sr), ("Notch", self.notch)):
            ml.addWidget(QLabel(lbl)); ml.addWidget(w)
        ml.addWidget(self.ch_btn); ml.addWidget(self.ch_lbl, 1)
        ml.addWidget(self.stream)
        self.quit_btn = QPushButton("Quit"); self.quit_btn.clicked.connect(self.close)
        ml.addWidget(self.quit_btn)
        root.addWidget(meas)
        self._update_ch_label()

        # LED 0/1
        self.led0 = LedPanel("LED 0", (0, 200, 255), on_live=lambda rgb, i: self._live_led(0, rgb, i))
        self.led1 = LedPanel("LED 1", (255, 120, 0), on_live=lambda rgb, i: self._live_led(1, rgb, i))
        leds = QHBoxLayout(); leds.addWidget(self.led0); leds.addWidget(self.led1)
        root.addLayout(leds)

        # 디바이스 + 캘리브레이션 + 스펙트럼
        mid = QHBoxLayout()
        dev = QGroupBox("Device — 동글로 연결 (calibration & live preview)"); dl = QVBoxLayout(dev)
        drow = QHBoxLayout()
        # 동글(USB) 경로. Mac 블루투스는 쓰지 않는다 — Pairer 와 동일한 BridgeTransport.
        self.port_box = QComboBox()
        port_refresh = QPushButton("↻"); port_refresh.setFixedWidth(32)
        port_refresh.clicked.connect(self._refresh_ports)
        self.dev_num = QSpinBox(); self.dev_num.setRange(1, 9999); self.dev_num.setValue(1)
        self.conn_btn = QPushButton("Connect"); self.conn_btn.clicked.connect(self._toggle_conn)
        self.conn_status = QLabel("Not connected"); self.conn_status.setStyleSheet("color:#8a9099;")
        self.calib_btn = QPushButton("Calibrate LEDs"); self.calib_btn.clicked.connect(self._calibrate_all)
        self.calib_btn.setEnabled(False)
        drow.addWidget(QLabel("Dongle")); drow.addWidget(self.port_box, 1); drow.addWidget(port_refresh)
        drow.addSpacing(12)
        drow.addWidget(QLabel("Headstage #")); drow.addWidget(self.dev_num)
        drow.addWidget(self.conn_btn)
        drow.addWidget(self.conn_status, 1); drow.addWidget(self.calib_btn)
        dl.addLayout(drow)
        self.calib_status = QLabel("Connect → set each LED's channel/band/baseline/mult → Calibrate LEDs "
                                   "(one baseline → each threshold = mean band-power × mult, auto)")
        self.calib_status.setStyleSheet("color:#8a9099;")
        dl.addWidget(self.calib_status)
        mid.addWidget(dev, 1)
        root.addLayout(mid)
        root.addWidget(QLabel("Preview (when connected · per-channel raw waveform | power spectrum 0–60Hz · LED0/LED1 below)"))
        root.addWidget(self.scope, 1)

        # Build / save (Generate then Save)
        build = QGroupBox("Firmware build / save"); bl = QHBoxLayout(build)
        self.name_edit = QLineEdit("cbrain_custom")
        self.base_lbl = QLabel(os.path.basename(self.base_hex)); self.base_lbl.setStyleSheet("color:#8a9099;")
        base_btn = QPushButton("base.hex…"); base_btn.clicked.connect(self._pick_base)
        load_btn = QPushButton("📂 Load .hex…"); load_btn.clicked.connect(self._load_hex)
        load_btn.setToolTip("기존에 만든 펌웨어에서 설정(샘플레이트·채널·LED 밴드·임계값)을 불러옵니다")
        self.gen_btn = QPushButton("⚙ Generate firmware"); self.gen_btn.clicked.connect(self._generate)
        self.save_btn = QPushButton("💾 Save…"); self.save_btn.clicked.connect(self._save); self.save_btn.setEnabled(False)
        bl.addWidget(QLabel("Name")); bl.addWidget(self.name_edit)
        bl.addWidget(base_btn); bl.addWidget(self.base_lbl)
        bl.addWidget(load_btn); bl.addStretch()
        bl.addWidget(self.gen_btn); bl.addWidget(self.save_btn)
        root.addWidget(build)
        self.status = QLabel("Configure → 'Generate firmware' → 'Save'")
        root.addWidget(self.status)
        self.resize(1120, 860)
        self._target = 1
        self._refresh_ports()          # 동글 포트 자동 탐지

    # ── 채널 선택 (#1) ──
    def _select_channels(self):
        dlg = QDialog(self); dlg.setWindowTitle("Select recording channels (IN0–IN7)")
        g = QGridLayout(dlg)
        boxes = []
        for i in range(N_INPUTS):
            cb = QCheckBox(f"IN{i}"); cb.setChecked(i in self._channels)
            g.addWidget(cb, i // 4, i % 4); boxes.append(cb)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        g.addWidget(bb, 2, 0, 1, 4)
        if dlg.exec() == QDialog.Accepted:
            sel = [i for i, cb in enumerate(boxes) if cb.isChecked()] or [0]
            self._channels = sel
            self.scope.set_channels(len(sel))
            self._update_ch_label()

    def _update_ch_label(self):
        self.ch_lbl.setText("Channels: " + ", ".join(f"IN{c}" for c in self._channels) +
                            f"  ({len(self._channels)})")

    # ── config 조립 ──
    def to_config(self) -> FwConfig:
        return FwConfig(
            sr_hz=SR_CHOICES[self.sr.currentIndex()], notch=self.notch.currentData(),
            ch_count=len(self._channels), stream_ble=self.stream.isChecked(),
            ch_map=list(self._channels),
            leds=[self.led0.to_config(), self.led1.to_config()])

    def _load_hex(self):
        """기존에 만든 펌웨어(.hex)에서 설정을 읽어 UI 에 채운다 (조건 확인/재사용)."""
        from cbrain_studio.app.fw_config import BLOB_SIZE
        from cbrain_studio.app.hex_stamp import read_region
        p, _ = QFileDialog.getOpenFileName(self, "펌웨어 .hex 열기 (설정 읽기)",
                                           _last_dir(os.path.dirname(self.base_hex)), "HEX (*.hex)")
        if not p:
            return
        _remember_dir(p)
        try:
            with open(p) as f:
                cfg = FwConfig.from_bytes(read_region(f.read(), CB_CONFIG_ADDR, BLOB_SIZE))
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"❌ 설정을 읽지 못했습니다 ({os.path.basename(p)}): {e}")
            return
        if cfg is None:
            self.status.setText(f"❌ 설정 blob 이 없습니다: {os.path.basename(p)}")
            return
        # UI 채우기
        self.sr.setCurrentText(f"{cfg.sr_hz} Hz")
        i = self.notch.findData(cfg.notch)
        if i >= 0:
            self.notch.setCurrentIndex(i)
        self.stream.setChecked(bool(cfg.stream_ble))
        self._channels = list(cfg.ch_map)[:cfg.ch_count] or [0]
        self._update_ch_label()
        for panel, lc in zip((self.led0, self.led1), cfg.leds):
            panel.from_config(lc)
        self.name_edit.setText(os.path.splitext(os.path.basename(p))[0])
        self.status.setText(f"✅ 설정 불러옴: {os.path.basename(p)}  "
                            f"(sr={cfg.sr_hz}Hz, {cfg.ch_count}ch, thr={cfg.leds[0].threshold:.0f}"
                            f"/{cfg.leds[1].threshold:.0f})")

    # ── 빌드: 생성 / 저장 분리 (#3) ──
    def _pick_base(self):
        p, _ = QFileDialog.getOpenFileName(self, "base.hex 선택",
                                           _last_dir(os.path.dirname(self.base_hex)), "HEX (*.hex)")
        _remember_dir(p)
        if p:
            self.base_hex = p; self.base_lbl.setText(os.path.basename(p))

    def _generate(self):
        if not os.path.exists(self.base_hex):
            self.status.setText(f"❌ base.hex not found: {self.base_hex}"); self.save_btn.setEnabled(False); return
        try:
            with open(self.base_hex) as f:
                base = f.read()
            blob = self.to_config().to_bytes()
            out_hex = stamp_config(base, blob, CB_CONFIG_ADDR)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"❌ Generate failed: {e}"); self.save_btn.setEnabled(False); return
        self._built = (out_hex, blob)
        self.save_btn.setEnabled(True)
        self.status.setText(f"✅ Generated ({len(blob)} B config @ 0x{CB_CONFIG_ADDR:X}) — click 'Save'")

    def _save(self):
        if self._built is None:
            self.status.setText("Click 'Generate firmware' first"); return
        out_hex, blob = self._built
        name = self.name_edit.text() or "cbrain_custom"
        default = os.path.join(_last_dir(os.path.expanduser("~")), f"{name}.hex")
        path, _ = QFileDialog.getSaveFileName(self, "Save firmware", default, "HEX (*.hex)")
        if not path:
            return
        _remember_dir(path)
        with open(path, "w") as f:
            f.write(out_hex)
        try:
            import json
            from dataclasses import asdict
            with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as f:
                json.dump(asdict(self.to_config()), f, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        self.status.setText(f"💾 Saved: {path}  → flash with CBRAIN Flasher")

    # ── 디바이스/캘리브/live LED (연결 시) ──
    def _live_led(self, idx, rgb, intensity):
        if self._ble_state == "connected":
            r, g, b = (c * intensity // 100 for c in rgb)   # 강도 미리 반영 → 기기 PWM 듀티
            self._send(packet.cmd_set_led(idx, r, g, b))

    def _toggle_conn(self):
        if self._ble_state in ("idle", "error"):
            self._connect()
        else:
            self._disconnect()

    def _refresh_ports(self):
        """연결된 cb_bridge 동글 포트 목록 갱신 (데이터 포트는 번호 큰 쪽)."""
        self.port_box.clear()
        ports = find_bridge_ports()
        if ports:
            self.port_box.addItems(ports)
            self.port_box.setCurrentIndex(len(ports) - 1)
        else:
            self.port_box.addItem("(동글 없음)")

    def _connect(self):
        """동글(USB)로 연결. Mac 블루투스는 쓰지 않는다."""
        from cbrain_studio.app.acquisition import AcquisitionManager
        ports = find_bridge_ports()
        if not ports:
            self.conn_status.setText("❌ 동글 없음 — USB 확인 후 ↻")
            return
        port = self.port_box.currentText()
        if port not in ports:
            port = ports[-1]
        self._target = self.dev_num.value()

        self.tx = BridgeTransport(port)
        self.acq = AcquisitionManager(self.tx); self.acq.start()
        self.acq.bus.subscribe(self.scope.push)
        self.acq.bus.subscribe(self._on_block)
        self._ble_state = "connecting"; self._ble_err = ""
        self._loop = asyncio.new_event_loop(); self._run = True
        threading.Thread(target=self._ble_run, daemon=True).start()
        self.draw_timer.start(DRAW_MS); self.conn_btn.setText("Disconnect")

    def _disconnect(self):
        self._run = False; self._ble_state = "idle"
        self.conn_btn.setText("Connect")
        self.calib_btn.setEnabled(False)
        self.conn_status.setText("Not connected")

    def _on_block(self, block):
        # 동글이 지정 기기에 붙어 데이터가 오기 시작하면 그때가 '연결됨'
        if self._ble_state != "connected":
            self._ble_state = "connected"
        if block.channel_count != self.scope.ch:
            self.scope.set_channels(block.channel_count)

    def _ble_run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ble_main())
        except Exception as e:  # noqa: BLE001
            self._ble_err = f"{type(e).__name__}: {e}"; self._ble_state = "error"

    async def _ble_main(self):
        while self._run:
            try:
                await self.tx.open()                       # 동글 CDC 열기
            except Exception as e:  # noqa: BLE001
                self._ble_err = str(e); self._ble_state = "retry"; await asyncio.sleep(2); continue
            # 동글에 대상 헤드스테이지 각인(페어링) → 실제 연결까지 15~20초 걸림
            await self.tx.send(set_target_frame(self._target))
            await asyncio.sleep(0.5)
            await self.acq.send_command(packet.cmd_get_caps())
            while self._run and self.tx.is_open:
                await asyncio.sleep(0.2)
            if not self._run:
                break
            self._ble_state = "retry"
        await self.tx.close()

    def _send(self, payload):
        loop = getattr(self, "_loop", None)
        if loop is not None and self._ble_state == "connected":
            asyncio.run_coroutine_threadsafe(self.tx.send(payload), loop)

    def _calibrate_all(self):
        """활성 LED 전부를 한 번의 baseline 으로 캘리브레이션(각자 대역/채널/mult). 임계 자동."""
        if self._ble_state != "connected" or self._calibs:
            return
        sr = SR_CHOICES[self.sr.currentIndex()]
        now = time.monotonic()
        self._calibs = []
        for idx, panel in ((0, self.led0), (1, self.led1)):
            if not panel.enabled.isChecked():
                continue
            ch = panel.channel.value()
            row = self._channels.index(ch) if ch in self._channels else 0
            gate = BandPowerGate(sr, panel.lo.value(), panel.hi.value(),
                                 fft_n(sr, panel.win.value()), k=0.0, channels=[row])
            self._calibs.append(dict(idx=idx, gate=gate, end=now + panel.baseline.value(),
                                     mult=panel.mult.value(), row=row))
        if not self._calibs:
            self.calib_status.setText("No enabled LED to calibrate")
            return
        self.calib_status.setText(f"Calibrating {len(self._calibs)} LED(s)… (band-power only)")

    # ── 그리기/상태 (메인 스레드) ──
    def _eval_led(self, panel) -> bool:
        """샘플-단위 게이트 근사: 직전 window 구간 대역파워 > 절대임계 → ON."""
        if not panel.enabled.isChecked() or panel.thr.value() <= 0:
            return False
        ch = panel.channel.value()
        if ch not in self._channels:
            return False
        row = self._channels.index(ch)
        if row >= self.scope.ch:
            return False
        sr = SR_CHOICES[self.sr.currentIndex()]
        win = fft_n(sr, panel.win.value())              # pow2, 펌웨어와 동일
        seg = self.scope.buf[row, -win:]
        return band_power(seg, sr, panel.lo.value(), panel.hi.value()) > panel.thr.value()

    def _tick(self):
        self.scope.update()
        for idx, panel in ((0, self.led0), (1, self.led1)):     # 라이브 LED0/LED1
            self.scope.set_led(idx, self._eval_led(panel))
        st = self._ble_state
        self.conn_status.setText({"connecting": f"CBRAIN_{getattr(self,'_target','?')} 연결 대기… (15~20초)",
                                  "retry": f"동글 오류, 재시도… ({self._ble_err[:30]})",
                                  "error": f"Error: {self._ble_err[:40]}", "connected": "Connected"}.get(st, "Not connected"))
        if self._calibs:
            now = time.monotonic()
            still = []
            for cb in self._calibs:
                cb["gate"].add_calib(self.scope.buf)
                if now >= cb["end"]:
                    cb["gate"].finalize()   # 대역 파워 평균 (k=0)
                    row, g = cb["row"], cb["gate"]
                    mp = g.threshold[row] if g.threshold is not None and row < len(g.threshold) else 0.0
                    thr = float(mp * cb["mult"])
                    (self.led0 if cb["idx"] == 0 else self.led1).thr.setValue(thr)
                else:
                    still.append(cb)
            done = len(self._calibs) - len(still)
            self._calibs = still
            if not still:
                self.calib_status.setText("✅ Calibrated — thresholds = mean band-power × mult (auto-applied)")
            else:
                remain = max(c["end"] for c in still) - now
                self.calib_status.setText(f"Calibrating… {remain:.0f}s left ({done} done)")
        self.calib_btn.setEnabled(st == "connected" and not self._calibs)   # 공용 버튼(처리 후 갱신)

    def closeEvent(self, e):
        self._run = False; e.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()


def run() -> int:
    import sys

    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    w = FwBuilderWindow()
    scr = app.primaryScreen().availableGeometry()      # 화면에 맞춰
    w.resize(int(scr.width() * 0.92), int(scr.height() * 0.92))
    w.move(scr.center() - w.rect().center())
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
