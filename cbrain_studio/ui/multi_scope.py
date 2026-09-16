"""
MultiScope — up to 8 devices stacked in one window, shared time axis. Display-only.

Each device panel = selected channels + LED1/LED2 lanes. Draws DisplayBuffer.snapshot().
Rendered on a **GUI timer (~30 Hz)**, not per data block. Draw cost scales with pixel
count (the data is downsampled). Design: docs/cbrain_studio_architecture.md §3.5.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

CH_COLORS = [QColor(c) for c in ("#4fc3f7", "#81c784", "#ffb74d", "#e57373",
                                 "#ba68c8", "#4db6ac", "#fff176", "#a1887f")]
LED_COLORS = [QColor("#00c8ff"), QColor("#ff7828")]
PANEL_MIN_H = 90


class MultiScope(QWidget):
    """Draws several devices stacked. panels are (label, DisplayBuffer, state_fn) tuples."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(200)
        self._panels: list[tuple] = []      # (label, display_buffer, state_fn)
        self.seconds = 10.0
        self.disp_filter = None
        self.panel_h = 150
        self.uv_per_lsb = 0.195
        self.setAutoFillBackground(True)

    def set_panels(self, panels: list[tuple]) -> None:
        self._panels = panels
        self._resize()

    def set_window(self, seconds: float) -> None:
        self.seconds = seconds

    def set_filter(self, f) -> None:
        self.disp_filter = f

    def set_panel_height(self, h: int) -> None:
        self.panel_h = max(PANEL_MIN_H, h)
        self._resize()

    def _resize(self):
        self.setMinimumHeight(max(200, len(self._panels) * self.panel_h))

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(900, max(200, len(self._panels) * self.panel_h))

    # paint (GUI-thread timer calls update())
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#15181c"))
        w = self.width()
        px = max(64, w - 90)                       # exclude the left label area
        for i, (label, db, state_fn) in enumerate(self._panels):
            top = i * self.panel_h
            self._paint_panel(p, label, db, state_fn, 0, top, w, self.panel_h, px)
        p.end()

    def _paint_panel(self, p, label, db, state_fn, x, y, w, h, px):
        # background / divider
        p.fillRect(x, y, w, h, QColor("#1b1f24") if (y // self.panel_h) % 2 == 0 else QColor("#171a1e"))
        p.setPen(QPen(QColor("#2a2f36"))); p.drawLine(x, y + h - 1, x + w, y + h - 1)

        snap = db.snapshot(self.seconds, px, self.uv_per_lsb, self.disp_filter)
        # header
        st = state_fn() if state_fn else ""
        p.setPen(QColor("#e0e0e0"))
        p.drawText(x + 8, y + 16, f"▸ {label}   {st}")

        if snap is None:
            p.setPen(QColor("#6a7078"))
            p.drawText(x + 40, y + h // 2, "Waiting (no data)")
            return

        ch = snap["ch"]
        led = snap["led"]
        n_led = led.shape[0]
        led_meas = snap.get("led_real", False)      # device-reported vs host-estimated
        plot_x = x + 84
        plot_w = w - plot_x - 6
        # split the signal area from the LED lanes (each LED lane = 18px)
        led_lane = 18
        led_h = led_lane * n_led if n_led else 0
        sig_top = y + 22
        sig_h = h - 22 - led_h - 4
        bh = sig_h / max(1, ch)

        # ── grid: time verticals + per-channel zero line ──
        self._draw_grid(p, plot_x, sig_top, plot_w, sig_h, ch, bh)

        # channel waveforms (min/max band)
        lo, hi = snap["lo"], snap["hi"]
        xs = np.linspace(plot_x, plot_x + plot_w, lo.shape[1])
        for c in range(ch):
            base = sig_top + bh * (c + 0.5)
            amp = max(1.0, float(np.max(np.abs([lo[c], hi[c]]))))     # per-channel auto-scale
            scale = (bh * 0.45) / amp
            col = CH_COLORS[c % len(CH_COLORS)]
            p.setPen(QColor("#8a9099"))
            p.drawText(x + 8, int(base) + 4, f"ch{snap['order'][c] if c < len(snap['order']) else c}")
            p.setPen(QPen(col, 1))
            ylo = base - lo[c] * scale
            yhi = base - hi[c] * scale
            for k in range(len(xs)):
                p.drawLine(int(xs[k]), int(yhi[k]), int(xs[k]), int(ylo[k]))

        # ── LED lanes: ON spans drawn as filled blocks, OFF as a thin baseline ──
        for li in range(n_led):
            ly = y + h - led_h + led_lane * li
            on = led[li]
            col = LED_COLORS[li % len(LED_COLORS)]
            frac = float(on.mean()) if on.size else 0.0
            p.setPen(QColor("#8a9099"))
            p.drawText(x + 8, ly + 12, f"LED{li + 1}")
            base_y = ly + led_lane - 3          # OFF baseline
            top_y = ly + 3                      # ON ceiling
            # OFF baseline
            p.setPen(QPen(QColor("#3a4048"), 1)); p.drawLine(plot_x, base_y, plot_x + plot_w, base_y)
            # fill ON spans as rectangles
            p.setBrush(col); p.setPen(Qt.NoPen)
            k = 0
            while k < len(xs) - 1:
                if on[k]:
                    j = k
                    while j < len(xs) - 1 and on[j]:
                        j += 1
                    p.drawRect(int(xs[k]), top_y, max(1, int(xs[j]) - int(xs[k])), base_y - top_y)
                    k = j
                else:
                    k += 1
            p.setBrush(Qt.NoBrush)
            # ON-percentage label + source (device-measured vs host-estimated)
            p.setPen(col)
            p.drawText(plot_x + plot_w - 74, ly + 12,
                       f"{frac*100:.0f}% {'meas' if led_meas else 'est'}")

    def _draw_grid(self, p, x, y, w, h, ch, bh):
        """Vertical time grid + per-channel zero (center) line."""
        p.setPen(QPen(QColor("#242a31"), 1))
        # vertical time lines: divide `seconds` into 1-s steps (max 12)
        divs = min(12, max(2, int(self.seconds)))
        for i in range(divs + 1):
            gx = x + w * i / divs
            p.drawLine(int(gx), y, int(gx), y + h)
        # channel center (zero) line
        p.setPen(QPen(QColor("#20262c"), 1, Qt.DashLine))
        for c in range(ch):
            cy = y + bh * (c + 0.5)
            p.drawLine(x, int(cy), x + w, int(cy))
        # time labels (right = 0, left = -seconds)
        p.setPen(QColor("#5a6068"))
        for i in (0, divs):
            gx = x + w * i / divs
            t = -self.seconds * (1 - i / divs)
            p.drawText(int(gx) - (2 if i == divs else 0), y + h + 10, f"{t:.0f}s")
