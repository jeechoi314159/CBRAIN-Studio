"""
BuilderScope — Firmware Builder 미리보기.

채널마다 [raw 파형 (좌, 5) | 실시간 스펙트로그램 (우, 1)] 을 나란히, 그리고 하단에
**LED0 · LED1** 게이트 상태를 사각파로 표시. 스펙트로그램은 보이는 시간창을 겹치는
윈도우로 FFT 해 매 갱신마다 다시 계산(주파수×시간 히트맵).
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from cbrain_studio.core.types import UV_PER_LSB

BUF = 4096
FMAX = 60
SPEC_WIN = 512          # x_samples 최소 창
TRACE = [QColor(c) for c in ("#4fc3f7", "#81c784", "#ffb74d", "#e57373",
                             "#ba68c8", "#4db6ac", "#fff176", "#a1887f")]
AXIS, LABEL, BG = QColor("#3a4048"), QColor("#8a9099"), QColor("#121417")
ML, MB, LED_H, WAVE_FRAC = 50, 16, 22, 5 / 6


class BuilderScope(QWidget):
    def __init__(self, ch_count: int, sr_hz: int = 1024):
        super().__init__()
        self.setMinimumSize(760, 360)
        self.sr = sr_hz
        self.x_window_ms = 2000
        self.set_channels(ch_count)

    def set_channels(self, ch_count: int):
        self.ch = max(1, ch_count)
        self.buf = np.zeros((self.ch, BUF), dtype=float)
        self.total = 0
        self.led_trans = [[], []]          # LED0, LED1 → [(pos, state)]
        self._led_state = [0, 0]
        self.update()

    def push(self, block):                 # BLE 스레드
        if block.channel_count != self.ch:
            return
        n = block.n_samples
        nb = np.roll(self.buf, -n, axis=1)
        nb[:, -n:] = block.channels.astype(float)
        self.buf = nb
        self.total += n

    def set_led(self, idx: int, state: bool):   # 메인 스레드
        s = 1 if state else 0
        if s != self._led_state[idx]:
            self._led_state[idx] = s
            self.led_trans[idx].append((self.total, s))

    def x_samples(self) -> int:
        return max(SPEC_WIN + 8, min(BUF, int(self.x_window_ms * self.sr / 1000)))

    # ── 렌더 ──
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        w, h = self.width(), self.height()
        buf, total = self.buf, self.total
        xs_n = self.x_samples()
        plot_w = w - ML
        wave_w = plot_w * WAVE_FRAC
        spec_x = ML + wave_w
        spec_w = plot_w - wave_w
        led_area = 2 * LED_H
        lanes_h = h - MB - led_area
        bh = lanes_h / self.ch
        left = total - xs_n
        win = buf[:, -xs_n:]
        xs = ML + np.arange(xs_n) * (wave_w / xs_n)

        for c in range(self.ch):
            top = c * bh
            center = top + bh / 2
            uv = win[c] * UV_PER_LSB
            span = max(10.0, float(np.abs(uv).max()) * 1.1)
            amp = (bh / 2) * 0.85 / span
            p.setPen(QPen(AXIS, 1)); p.drawLine(ML, int(center), int(spec_x), int(center))
            p.setPen(LABEL); p.drawText(4, int(top) + 12, f"ch{c}")
            p.drawText(4, int(center) - 2, f"±{span:.0f}µV")
            ys = center - uv * amp
            poly = QPolygonF([QPointF(float(xs[i]), float(ys[i])) for i in range(0, xs_n, 2)])
            p.setPen(QPen(TRACE[c % len(TRACE)], 1.1)); p.drawPolyline(poly)
            self._paint_spectrum(p, win[c], int(spec_x), int(top), int(spec_w), int(bh),
                                 TRACE[c % len(TRACE)])

        # 시간축
        p.setPen(LABEL)
        secs = xs_n / self.sr
        for i in range(5):
            x = ML + int(i / 4 * wave_w)
            p.drawText(x, int(lanes_h) + 12, f"{-secs * (1 - i / 4):.1f}s")
        p.drawText(int(spec_x) + 2, int(lanes_h) + 12, "0")
        p.drawText(int(w) - 26, int(lanes_h) + 12, f"{FMAX}Hz")

        # LED 레인 ×2 (파형 x 영역에 정렬)
        for idx in range(2):
            y0 = lanes_h + MB + idx * LED_H
            self._paint_led(p, idx, ML, y0, wave_w, LED_H, left, xs_n)
        p.end()

    def _paint_spectrum(self, p, sig, x0, y0, w, h, color):
        """채널별 파워 스펙트럼 (선 그래프): X=주파수 0–FMAX, Y=크기."""
        if w < 8 or h < 6 or len(sig) < 16:
            return
        n = len(sig)
        freqs = np.fft.rfftfreq(n, 1.0 / self.sr)
        keep = freqs <= FMAX
        fx = freqs[keep]
        mag = np.abs(np.fft.rfft((sig - sig.mean()) * np.hanning(n)))[keep]
        mx = float(mag.max()) or 1.0
        base = y0 + h - 3
        # 격자 (10 Hz 간격)
        p.setPen(QPen(AXIS, 1))
        for fhz in range(0, FMAX + 1, 10):
            gx = x0 + int(fhz / FMAX * w)
            p.drawLine(gx, y0 + 2, gx, int(base))
        # 스펙트럼 선
        poly = QPolygonF([QPointF(float(x0 + fx[i] / FMAX * w),
                                  float(base - mag[i] / mx * (h - 6)))
                          for i in range(len(fx))])
        p.setPen(QPen(color, 1.3)); p.drawPolyline(poly)
        pk = fx[int(np.argmax(mag))] if mag.size else 0.0
        p.setPen(LABEL); p.drawText(x0 + 3, y0 + 11, f"{pk:.0f}Hz")

    def _paint_led(self, p, idx, x0, y0, w, lh, left, xs_n):
        hi, lo = y0 + 4, y0 + lh - 4
        col = QColor("#ffca28" if idx == 0 else "#4fc3f7")
        p.setPen(LABEL); p.drawText(4, int(y0) + 15, f"LED{idx}")
        trans = [(pos, s) for (pos, s) in self.led_trans[idx] if pos >= left - xs_n]
        self.led_trans[idx] = trans
        state = 0
        for pos, s in trans:
            if pos <= left:
                state = s

        def xp(pos): return x0 + (pos - left) * (w / xs_n)
        p.setPen(QPen(col, 1.8))
        cx, cy = x0, (hi if state else lo)
        for pos, s in trans:
            if pos <= left:
                continue
            px = xp(pos); ny = hi if s else lo
            p.drawLine(int(cx), int(cy), int(px), int(cy))
            p.drawLine(int(px), int(cy), int(px), int(ny))
            cx, cy = px, ny
        p.drawLine(int(cx), int(cy), x0 + int(w), int(cy))
