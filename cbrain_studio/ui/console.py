"""
CBRAIN Studio — 단일기기 획득 콘솔 (블럭화).

영역을 블럭으로 분리해 확장 가능하게 구성한다:
  Profile · Device · Channels · Frequency-to-lit · Display · Recording.

frequency-to-lit 폐루프: 최근 250 ms 를 FFT 해 대역 파워가 캘리브레이션 threshold 를
넘으면 LED 점등(BandPowerGate). 화면에는 화살표 대신 **사각파**로 표시.

파이프라인은 코어 재사용: BleakTransport → AcquisitionManager → SampleBus →
{Scope, DeviceRecorder}; 게이트는 메인 스레드에서 Scope 버퍼(최근 창)로 판정.
bleak 은 백그라운드 스레드, Qt 는 메인 스레드. **터미널 블루투스 권한** 필요.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from cbrain_studio.app.acquisition import AcquisitionManager
from cbrain_studio.app.led_gate import BandPowerGate
from cbrain_studio.app.profile import Profile
from cbrain_studio.app.recording import DeviceRecorder
from cbrain_studio.core import packet
from cbrain_studio.core.types import UV_PER_LSB, EventMarker
from cbrain_studio.ui.scope import Scope

SR, DRAW_MS, CALIB_WINDOWS = 1024, 33, 24     # 캘리브레이션: 비중첩 250ms 창 24개(~6s)


class ConsoleWindow(QWidget):
    def __init__(self, profile: Profile | None = None, out_dir: str = "./recordings"):
        super().__init__()
        self.setWindowTitle("CBRAIN Studio — 획득 콘솔")
        self.out_dir = out_dir
        # Finder 실행 시 CWD='/' 라 상대경로 쓰기가 막힌다 → 쓰기 가능한 곳으로.
        self._profile_dir = ("./profiles" if os.access(".", os.W_OK)
                             else os.path.expanduser("~/CBRAIN_profiles"))
        self.profile = profile or Profile()

        self.ch = self.profile.channels.count
        self._ble_state = "idle"
        self._ble_error = ""
        self._caps = None
        self.phase = "idle"                    # idle|calibrating|detecting
        self.gate: BandPowerGate | None = None
        self._last_calib_total = 0
        self._device_id = 1
        self._recorder = None
        self._rec_req = None
        self._rec_meta = {}
        self._rec_gaps = 0

        self.scope = Scope(self.ch, SR)
        self._build_ui()
        self._apply_profile_to_ui(self.profile)
        self._apply_display()
        self._set_controls_enabled(False)

        self.draw_timer = QTimer(self); self.draw_timer.timeout.connect(self._tick)

    # ══ UI 블럭 ══
    def _build_ui(self):
        root = QVBoxLayout(self)
        row1 = QHBoxLayout()
        row1.addWidget(self._block_profile()); row1.addWidget(self._block_device(), 1)
        self.quit_btn = QPushButton("종료"); self.quit_btn.clicked.connect(self._quit)
        self.quit_btn.setStyleSheet("padding:6px 14px;")
        row1.addWidget(self.quit_btn)
        root.addLayout(row1)
        root.addWidget(self.scope, 1)
        row2 = QHBoxLayout()
        row2.addWidget(self._block_channels()); row2.addWidget(self._block_freqlit())
        row2.addWidget(self._block_display()); row2.addWidget(self._block_recording(), 1)
        root.addLayout(row2)
        self.resize(1040, 720)

    def _block_profile(self):
        g = QGroupBox("프로파일"); l = QHBoxLayout(g)
        self.prof_name = QLineEdit(self.profile.name)
        load = QPushButton("불러오기"); load.clicked.connect(self._load_profile)
        save = QPushButton("저장"); save.clicked.connect(self._save_profile)
        l.addWidget(self.prof_name); l.addWidget(load); l.addWidget(save)
        return g

    def _block_device(self):
        g = QGroupBox("디바이스"); l = QHBoxLayout(g)
        self.dev_name = QLineEdit(self.profile.device.name_prefix)
        self.conn_btn = QPushButton("연결"); self.conn_btn.clicked.connect(self._toggle_conn)
        self.conn_status = QLabel("정지됨")
        self.caps_lbl = QLabel("—"); self.caps_lbl.setStyleSheet("color:#8a9099;")
        l.addWidget(QLabel("이름:")); l.addWidget(self.dev_name)
        l.addWidget(self.conn_btn); l.addWidget(self.conn_status, 1); l.addWidget(self.caps_lbl)
        return g

    def _block_channels(self):
        g = QGroupBox("채널"); l = QHBoxLayout(g)
        self.ch_spin = QSpinBox(); self.ch_spin.setRange(1, 16); self.ch_spin.setValue(self.ch)
        self.ch_spin.valueChanged.connect(self._set_ch)
        l.addWidget(QLabel("수:")); l.addWidget(self.ch_spin)
        return g

    def _block_freqlit(self):
        g = QGroupBox("Frequency-to-lit (LED)"); l = QHBoxLayout(g)
        self.fl_on = QCheckBox("사용"); self.fl_on.setChecked(True)
        self.fl_lo = QDoubleSpinBox(); self.fl_lo.setRange(0.1, 500); self.fl_lo.setValue(8.0)
        self.fl_hi = QDoubleSpinBox(); self.fl_hi.setRange(0.2, 500); self.fl_hi.setValue(12.0)
        self.fl_k = QDoubleSpinBox(); self.fl_k.setRange(0.5, 10); self.fl_k.setValue(3.0); self.fl_k.setSingleStep(0.5)
        self.fl_led = QSpinBox(); self.fl_led.setRange(0, 1); self.fl_led.setValue(0)
        self.calib_btn = QPushButton("캘리브레이션"); self.calib_btn.clicked.connect(self._recalibrate)
        for lbl, wdg in (("대역:", self.fl_lo), ("–", self.fl_hi), ("Hz  k", self.fl_k), ("LED", self.fl_led)):
            l.addWidget(QLabel(lbl)); l.addWidget(wdg)
        l.addWidget(self.fl_on); l.addWidget(self.calib_btn)
        return g

    def _block_display(self):
        g = QGroupBox("디스플레이"); l = QHBoxLayout(g)
        self.view_btn = QPushButton("스펙트럼"); self.view_btn.setCheckable(True)
        self.view_btn.toggled.connect(self._toggle_view)
        self.auto_cb = QCheckBox("오토스케일"); self.auto_cb.setChecked(True)
        self.auto_cb.toggled.connect(self._apply_display)
        self.yr = QSpinBox(); self.yr.setRange(10, 5000); self.yr.setValue(200); self.yr.setSuffix("µV")
        self.yr.valueChanged.connect(self._apply_display)
        self.xw = QSpinBox(); self.xw.setRange(250, 4000); self.xw.setValue(2000); self.xw.setSingleStep(250); self.xw.setSuffix("ms")
        self.xw.valueChanged.connect(self._apply_display)
        l.addWidget(self.view_btn); l.addWidget(self.auto_cb)
        l.addWidget(QLabel("범위")); l.addWidget(self.yr)
        l.addWidget(QLabel("창")); l.addWidget(self.xw)
        return g

    def _block_recording(self):
        g = QGroupBox("기록"); l = QHBoxLayout(g)
        self.exp_edit = QLineEdit(); self.exp_edit.setPlaceholderText("실험명")
        self.animal_edit = QLineEdit(); self.animal_edit.setPlaceholderText("동물 ID")
        self.rec_btn = QPushButton("● 기록"); self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._toggle_record)
        self.rec_status = QLabel("—"); self.rec_status.setStyleSheet("color:#8a9099;")
        l.addWidget(QLabel("실험:")); l.addWidget(self.exp_edit)
        l.addWidget(QLabel("동물:")); l.addWidget(self.animal_edit)
        l.addWidget(self.rec_btn); l.addWidget(self.rec_status, 1)
        return g

    def _set_controls_enabled(self, on):
        for w in (self.ch_spin, self.calib_btn, self.rec_btn):
            w.setEnabled(on)

    # ══ 프로파일 ↔ UI ══
    def _apply_profile_to_ui(self, pr: Profile):
        self.prof_name.setText(pr.name)
        self.dev_name.setText(pr.device.name_prefix)
        self.ch_spin.setValue(pr.channels.count)
        self.fl_on.setChecked(pr.freq_to_lit.enabled)
        self.fl_lo.setValue(pr.freq_to_lit.band_lo_hz)
        self.fl_hi.setValue(pr.freq_to_lit.band_hi_hz)
        self.fl_k.setValue(pr.freq_to_lit.k)
        self.fl_led.setValue(pr.freq_to_lit.led_index)
        self.auto_cb.setChecked(pr.display.y_autoscale)
        self.yr.setValue(int(pr.display.y_range_uv))
        self.xw.setValue(pr.display.x_window_ms)
        self.view_btn.setChecked(pr.display.mode == "spectrum")

    def _read_profile_from_ui(self) -> Profile:
        pr = Profile(name=self.prof_name.text() or "default", calib_k=self.fl_k.value())
        pr.device.name_prefix = self.dev_name.text() or "CBRAIN"
        pr.channels.count = self.ch_spin.value()
        pr.channels.order = list(range(self.ch_spin.value()))
        pr.freq_to_lit.enabled = self.fl_on.isChecked()
        pr.freq_to_lit.band_lo_hz = self.fl_lo.value()
        pr.freq_to_lit.band_hi_hz = self.fl_hi.value()
        pr.freq_to_lit.k = self.fl_k.value()
        pr.freq_to_lit.led_index = self.fl_led.value()
        pr.display.y_autoscale = self.auto_cb.isChecked()
        pr.display.y_range_uv = float(self.yr.value())
        pr.display.x_window_ms = self.xw.value()
        pr.display.mode = "spectrum" if self.view_btn.isChecked() else "wave"
        return pr

    def _load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "프로파일 불러오기", self._profile_dir, "JSON (*.json)")
        if path:
            self.profile = Profile.load(path)
            self._apply_profile_to_ui(self.profile)
            self._apply_display()

    def _save_profile(self):
        pr = self._read_profile_from_ui()
        default = os.path.join(self._profile_dir, f"{pr.name}.json")
        path, _ = QFileDialog.getSaveFileName(self, "프로파일 저장", default, "JSON (*.json)")
        if path:
            pr.save(path)
            self.profile = pr

    # ══ 디스플레이 ══
    def _apply_display(self, *_):
        self.scope.apply_display(
            y_autoscale=self.auto_cb.isChecked(), y_range_uv=float(self.yr.value()),
            x_window_ms=self.xw.value(), led_lane=self.fl_on.isChecked())
        self.yr.setEnabled(not self.auto_cb.isChecked())

    def _toggle_view(self, on):
        self.scope.apply_display(mode="spectrum" if on else "wave")
        self.view_btn.setText("파형" if on else "스펙트럼")

    # ══ 획득/게이트 ══
    def _new_gate(self) -> BandPowerGate:
        win = int(SR * 0.25)          # 250 ms
        chans = None
        return BandPowerGate(SR, self.fl_lo.value(), self.fl_hi.value(), win,
                             k=self.fl_k.value(), channels=chans)

    def _begin_calibration(self):
        self.gate = self._new_gate() if self.fl_on.isChecked() else None
        self.phase = "calibrating" if self.gate else "detecting"   # 게이트 없으면 캘리브 불필요
        self._last_calib_total = self.scope.total
        self._set_led(False)

    def _recalibrate(self):
        self._begin_calibration()

    def _consume(self, block):            # BLE 스레드: 채널 적응 + 기록
        self._device_id = block.device_id
        if block.channel_count != self.ch:
            self.ch = block.channel_count
            self.scope.set_channels(self.ch)
            self._begin_calibration()
        self._handle_recording(block)

    def _handle_recording(self, block):
        if self._rec_req == "start" and self._recorder is None:
            meta = {**self._rec_meta, "enc": block.enc, "uv_per_lsb": UV_PER_LSB}
            rec = DeviceRecorder(self.out_dir, block.device_id, metadata=meta)
            rec.open(block.channel_count, block.sr_hz, block.order)
            self._recorder = rec; self._rec_req = None
        elif self._rec_req == "stop" and self._recorder is not None:
            self._recorder.close(); self._recorder = None; self._rec_req = None
        rec = self._recorder
        if rec is not None:
            tb = self.acq.timebase(block.device_id)
            counters = list(range(block.first_counter, block.first_counter + block.n_samples))
            rec.write_block(block, counters, tb.sample_times(block.first_counter, block.n_samples))

    def _on_disc(self, dev, counter, host_time, missing):
        self.scope.add_disc(); self._rec_gaps += 1
        rec = self._recorder
        if rec is not None:
            rec.add_discontinuity(counter, host_time, missing)

    def _on_caps(self, caps):
        self._caps = caps

    def _gate_step(self):
        """메인 스레드: 캘리브레이션 누적 / 게이트 판정 → LED + 사각파 + 마커."""
        if self.gate is None:
            return
        buf, total = self.scope.buf, self.scope.total
        if self.phase == "calibrating":
            if total >= self.gate.window and total - self._last_calib_total >= self.gate.window:
                self.gate.add_calib(buf)
                self._last_calib_total = total
                if self.gate.n_calib >= CALIB_WINDOWS:
                    self.gate.finalize()
                    self.phase = "detecting"
        elif self.phase == "detecting" and self.gate.ready:
            self._set_led(self.gate.active(buf))

    def _set_led(self, on: bool):
        on = bool(on)
        if on == (self.scope._led_state == 1):
            return                         # 변화 없음
        self.scope.set_led(on)
        idx = self.fl_led.value()
        self._send_ble(packet.cmd_set_led(idx, 255, 0, 0) if on
                       else packet.cmd_set_led(idx, 0, 0, 0))
        rec = self._recorder
        if rec is not None:
            rec.add_marker(EventMarker(self._device_id, self.scope.total, time.monotonic(),
                                       code=packet.CMD_SET_LED, value=1 if on else 0,
                                       label="freq_to_lit"))

    # ══ 연결 콜백 ══
    def _toggle_conn(self):
        if self._ble_state in ("idle", "error"):
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        from cbrain_studio.hal.bleak_transport import BleakTransport
        self.tx = BleakTransport(name_prefix=self.dev_name.text() or "CBRAIN",
                                 address=self.profile.device.address)
        self.acq = AcquisitionManager(self.tx)
        self.acq.start()
        self.acq.bus.subscribe(self.scope.push)
        self.acq.bus.subscribe(self._consume)
        self.acq.on_discontinuity = self._on_disc
        self.acq.on_capabilities = self._on_caps
        self._begin_calibration()
        self.draw_timer.start(DRAW_MS)
        self._start_ble()
        self.conn_btn.setText("연결 해제")

    def _disconnect(self):
        if self.rec_btn.isChecked():
            self.rec_btn.setChecked(False)
        self._stop_ble()
        self.draw_timer.stop()
        self._ble_state = "idle"
        self.conn_btn.setText("연결")
        self._set_controls_enabled(False)
        self.conn_status.setText("정지됨")

    def _set_ch(self, n):
        if self._ble_state == "connected":
            self._send_ble(packet.cmd_set_chmap(n))

    def _toggle_record(self, on):
        if on:
            self._rec_meta = {
                "experiment": self.exp_edit.text() or "(unnamed)",
                "animal_id": self.animal_edit.text() or "(unknown)",
                "firmware_version": (f"{self._caps.fw[0]}.{self._caps.fw[1]}.{self._caps.fw[2]}"
                                     if self._caps else "unknown"),
                "band_lo_hz": self.fl_lo.value(), "band_hi_hz": self.fl_hi.value(),
                "gate_k": self.fl_k.value(),
            }
            self._rec_gaps = 0; self._rec_req = "start"
            self.rec_btn.setText("■ 정지")
            self.exp_edit.setEnabled(False); self.animal_edit.setEnabled(False)
        else:
            self._rec_req = "stop"
            self.rec_btn.setText("● 기록")
            self.exp_edit.setEnabled(True); self.animal_edit.setEnabled(True)

    # ══ BLE 스레드 ══
    def _start_ble(self):
        self._ble_state = "connecting"; self._ble_error = ""
        self._ble_loop = asyncio.new_event_loop()
        self._ble_run_flag = True
        self._ble_thread = threading.Thread(target=self._ble_run, daemon=True)
        self._ble_thread.start()

    def _ble_run(self):
        asyncio.set_event_loop(self._ble_loop)
        try:
            self._ble_loop.run_until_complete(self._ble_main())
        except Exception as e:                 # noqa: BLE001
            self._ble_error = f"{type(e).__name__}: {e}"; self._ble_state = "error"
        finally:
            if self._recorder is not None:
                try:
                    self._recorder.close()
                except Exception:              # noqa: BLE001
                    pass
                self._recorder = None

    async def _ble_main(self):
        while self._ble_run_flag:
            try:
                await self.tx.open()
            except Exception as e:             # noqa: BLE001
                self._ble_attempts = getattr(self, "_ble_attempts", 0) + 1
                self._ble_error = f"{type(e).__name__}: {e}"; self._ble_state = "retry"
                await asyncio.sleep(2.0); continue
            self._ble_state = "connected"; self._ble_error = ""
            await self.acq.send_command(packet.cmd_get_caps())
            if self.ch_spin.value() != 4:
                await self.acq.send_command(packet.cmd_set_chmap(self.ch_spin.value()))
            while self._ble_run_flag and self.tx.is_open:
                await asyncio.sleep(0.2)
            if not self._ble_run_flag:
                break
            self._ble_state = "retry"
        await self.tx.close()

    def _stop_ble(self):
        self._ble_run_flag = False

    def _send_ble(self, payload: bytes):
        loop = getattr(self, "_ble_loop", None)
        if loop is not None and self._ble_state == "connected":
            asyncio.run_coroutine_threadsafe(self.tx.send(payload), loop)

    # ══ 그리기/상태 (메인 스레드) ══
    def _tick(self):
        self.scope.update()
        self._gate_step()
        st = self._ble_state
        if st == "connecting":
            self.conn_status.setText(f"'{self.dev_name.text()}…' 스캔/연결 중…")
        elif st == "retry":
            n = getattr(self, "_ble_attempts", 0)
            err = (self._ble_error or "").lower()
            if any(k in err for k in ("unauthor", "not authorized", "powered off", "turned off", "권한")):
                hint = "블루투스 권한/전원 확인 — 터미널에서 실행 권장(python tools/cbrain_console.py)"
            elif n >= 3:
                hint = "권한(서명 안 된 앱) 또는 다른 앱 점유 의심 — 터미널 실행/전원 재시작 확인"
            else:
                hint = "전원/다른 앱 확인"
            self.conn_status.setText(f"미발견 — 재시도 중 (#{n}). {hint}")
        elif st == "error":
            self.conn_status.setText(f"오류: {self._ble_error}")
        elif st == "connected":
            self._set_controls_enabled(True)
            if self.phase == "calibrating" and self.gate:
                nc = self.gate.n_calib
                self.conn_status.setText(f"연결됨 · 캘리브레이션 {nc}/{CALIB_WINDOWS}")
                self.scope.set_banner(f"CALIBRATING…  {nc}/{CALIB_WINDOWS}")
            else:
                self.scope.set_banner("")
                led = "ON" if self.scope._led_state else "off"
                gate = "off" if self.gate is None else f"{led}"
                self.conn_status.setText(f"연결됨 · 검출 중 · LED={gate} · gaps={self._rec_gaps}")
        else:
            self.scope.set_banner("")
        if self._caps is not None:
            c = self._caps
            self.caps_lbl.setText(f"fw {c.fw[0]}.{c.fw[1]}.{c.fw[2]} · {c.max_channels}ch · "
                                  f"{c.led_count} LED · {c.rates[0] if c.rates else '?'}Hz")
        rec = self._recorder
        if rec is not None:
            self.rec_status.setText(f"● {rec.n_written} samples · gaps={self._rec_gaps} · "
                                    f"{os.path.basename(rec.path)}")
        elif not self.rec_btn.isChecked():
            self.rec_status.setText("—")

    def _quit(self):
        self.close()

    # (진입점은 파일 하단 run())

    def closeEvent(self, e):
        if self.rec_btn.isChecked():
            self.rec_btn.setChecked(False)     # 기록 중이면 안전 종료
        self._stop_ble()
        e.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()


def run() -> int:
    import sys

    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    out = (os.path.join(os.getcwd(), "recordings") if os.access(".", os.W_OK)
           else os.path.expanduser("~/CBRAIN_recordings"))     # Finder CWD='/' 대비
    w = ConsoleWindow(out_dir=out)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
