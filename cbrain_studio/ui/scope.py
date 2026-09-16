"""
Scope — 다채널 실시간 뷰 (파형/스펙트럼) with **µV·시간 축**, 오토스케일/절대 범위,
그리고 **LED 게이트 square-wave 레인**.

- Y축: ADC 원값을 µV(UV_PER_LSB)로. autoscale(창 내 최대) 또는 절대(±y_range_uv).
- X축: 시간(초), 표시 창 = x_window_ms.
- LED: frequency-to-lit 게이트 on/off 를 화살표 대신 **사각파**로 하단 레인에 표시.
- 버퍼는 raw int16 로 유지(스레드 안전: 완성 배열 원자 스왑). 시각화는 스냅샷을 읽는다.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from cbrain_studio.core.types import UV_PER_LSB

BUF = 4096            # 링버퍼 최대 (4 s @ 1024 Hz)
TRACE = [QColor(c) for c in ("#4fc3f7", "#81c784", "#ffb74d", "#e57373",
                             "#ba68c8", "#4db6ac", "#fff176", "#a1887f")]
DISC_COLOR = QColor("#ff5252")
LED_COLOR = QColor("#ffca28")
AXIS = QColor("#3a4048")
LABEL = QColor("#8a9099")
BG = QColor("#121417")
ML, MB, LED_H = 52, 18, 34     # 좌 여백(Y라벨), 하 여백(시간), LED 레인 높이


class Scope(QWidget):
    def __init__(self, ch_count: int, sr_hz: int = 1024):
        super().__init__()
        self.setMinimumSize(780, 420)
        self.sr = sr_hz
        self.mode = "wave"
        self.y_autoscale = True
        self.y_range_uv = 200.0
        self.x_window_ms = 2000
        self.led_lane = True
        self.banner = ""          # 상단 오버레이 (예: 캘리브레이션 진행)
        self.set_channels(ch_count)

    def set_banner(self, text: str):
        if text != self.banner:
            self.banner = text
            self.update()

    # ── 설정 ──
    def apply_display(self, *, mode=None, y_autoscale=None, y_range_uv=None,
                      x_window_ms=None, led_lane=None):
        if mode is not None: self.mode = mode
        if y_autoscale is not None: self.y_autoscale = y_autoscale
        if y_range_uv is not None: self.y_range_uv = y_range_uv
        if x_window_ms is not None: self.x_window_ms = x_window_ms
        if led_lane is not None: self.led_lane = led_lane
        self.update()

    def set_channels(self, ch_count: int):
        self.ch = max(1, ch_count)
        self.buf = np.zeros((self.ch, BUF), dtype=float)
        self.total = 0
        self.discs: list[int] = []
        self.led_trans: list[tuple[int, int]] = []     # (sample_pos, state)
        self._led_state = 0
        self.update()

    # ── 데이터 (BLE 스레드) ──
    def push(self, block):
        if block.channel_count != self.ch:
            return
        n = block.n_samples
        nb = np.roll(self.buf, -n, axis=1)
        nb[:, -n:] = block.channels.astype(float)
        self.buf = nb
        self.total += n

    def add_disc(self):
        self.discs.append(self.total)

    # ── LED 게이트 상태 (메인 스레드) ──
    def set_led(self, state: bool):
        s = 1 if state else 0
        if s != self._led_state:
            self._led_state = s
            self.led_trans.append((self.total, s))

    def x_samples(self) -> int:
        return max(1, min(BUF, int(self.x_window_ms * self.sr / 1000)))

    # ── 렌더 ──
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        w, h = self.width(), self.height()
        buf, total = self.buf, self.total
        xs_n = self.x_samples()
        plot_w = w - ML
        led_h = LED_H if self.led_lane else 0
        plot_h = h - MB - led_h
        if self.mode == "spectrum":
            self._paint_spectrum(p, ML, 0, plot_w, plot_h + led_h, buf)
            self._time_axis(p, ML, plot_w, h, xs_n, spectrum=True)
            self._draw_banner(p, w)
            p.end(); return

        bh = plot_h / self.ch
        left = total - xs_n
        win = buf[:, -xs_n:]
        xs = ML + np.arange(xs_n) * (plot_w / xs_n)
        for c in range(self.ch):
            top = c * bh
            center = top + bh / 2
            uv = win[c] * UV_PER_LSB
            span = (self.y_range_uv if not self.y_autoscale
                    else max(10.0, float(np.abs(uv).max()) * 1.1))
            amp = (bh / 2) * 0.85 / span
            # 라벨/기준선
            p.setPen(QPen(AXIS, 1))
            p.drawLine(ML, int(center), w, int(center))
            p.setPen(LABEL)
            p.drawText(4, int(top) + 12, f"ch{c}")
            p.drawText(4, int(center) - 2, f"+{span:.0f}µV")
            p.drawText(4, int(top + bh) - 4, f"-{span:.0f}")
            # 파형
            ys = center - uv * amp
            poly = QPolygonF([QPointF(float(xs[i]), float(ys[i])) for i in range(0, xs_n, 2)])
            p.setPen(QPen(TRACE[c % len(TRACE)], 1.2))
            p.drawPolyline(poly)
        # 갭 마커
        self.discs = [m for m in list(self.discs) if m >= left]
        p.setPen(QPen(DISC_COLOR, 1.5))
        for m in self.discs:
            x = ML + int((m - left) * (plot_w / xs_n))
            p.drawLine(x, 0, x, int(plot_h))
        # LED 사각파 레인
        if self.led_lane:
            self._paint_led(p, ML, plot_h, plot_w, led_h, left, xs_n)
        self._time_axis(p, ML, plot_w, h, xs_n)
        self._draw_banner(p, w)
        p.end()

    def _draw_banner(self, p, w):
        if not self.banner:
            return
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QFont
        bw, bh = min(w - 20, 360), 40
        x = (w - bw) // 2
        p.fillRect(QRect(x, 8, bw, bh), QColor(255, 202, 40, 210))
        f = QFont(); f.setPointSize(14); f.setBold(True); p.setFont(f)
        p.setPen(QColor("#121417"))
        p.drawText(QRect(x, 8, bw, bh), 0x0084, self.banner)   # AlignCenter
        p.setFont(QFont())

    def _paint_led(self, p, x0, y0, w, lh, left, xs_n):
        hi, lo = y0 + 5, y0 + lh - 5
        p.setPen(QPen(AXIS, 1)); p.drawLine(x0, int(y0), x0 + int(w), int(y0))
        p.setPen(LABEL); p.drawText(4, int(y0) + 16, "LED")
        self.led_trans = [(pos, s) for (pos, s) in list(self.led_trans) if pos >= left - xs_n]
        # 창 시작 시점의 상태
        state = 0
        for pos, s in self.led_trans:
            if pos <= left:
                state = s
        def xp(pos): return x0 + (pos - left) * (w / xs_n)
        p.setPen(QPen(LED_COLOR, 1.8))
        cx = x0
        cy = hi if state else lo
        for pos, s in self.led_trans:
            if pos <= left:
                continue
            px = xp(pos)
            p.drawLine(int(cx), int(cy), int(px), int(cy))       # 수평
            ny = hi if s else lo
            p.drawLine(int(px), int(cy), int(px), int(ny))       # 수직(스텝)
            cx, cy, state = px, ny, s
        p.drawLine(int(cx), int(cy), x0 + int(w), int(cy))       # 현재까지

    def _time_axis(self, p, x0, w, h, xs_n, spectrum=False):
        if spectrum:
            return
        p.setPen(LABEL)
        secs = xs_n / self.sr
        n_ticks = 4
        for i in range(n_ticks + 1):
            frac = i / n_ticks
            x = x0 + int(frac * w)
            t = -secs * (1 - frac)
            p.drawText(x - 12 if i else x, h - 4, f"{t:.1f}s")

    def _paint_spectrum(self, p, x0, y0, w, h, buf):
        FMAX = 60
        bh = h / self.ch
        xs_n = self.x_samples()
        seg = buf[:, -xs_n:]
        freqs = np.fft.rfftfreq(xs_n, 1.0 / self.sr)
        keep = freqs <= FMAX
        fx = freqs[keep]
        win = np.hanning(xs_n)
        p.setPen(QPen(AXIS, 1))
        for fhz in range(10, FMAX + 1, 10):
            gx = x0 + int(fhz / FMAX * w)
            p.drawLine(gx, y0, gx, y0 + int(h))
        for c in range(self.ch):
            mag = np.abs(np.fft.rfft((seg[c] - seg[c].mean()) * win))[keep]
            base = y0 + (c + 1) * bh - 4
            norm = mag / (mag.max() or 1.0)
            poly = QPolygonF([QPointF(float(x0 + fx[i] / FMAX * w), float(base - norm[i] * (bh * 0.85)))
                              for i in range(len(fx))])
            p.setPen(QPen(TRACE[c % len(TRACE)], 1.4)); p.drawPolyline(poly)
            pk = fx[int(np.argmax(mag))] if mag.size else 0.0
            p.setPen(LABEL); p.drawText(x0 + 6, int(y0 + c * bh) + 14, f"ch{c}  peak={pk:.1f} Hz")
        p.setPen(LABEL)
        for fhz in range(0, FMAX + 1, 10):
            p.drawText(x0 + int(fhz / FMAX * w) + 2, y0 + int(h) - 4, f"{fhz}")
