"""
표시 전용 링버퍼 + 다운샘플 + 표시 필터 + LED 재현.

**원칙 (docs/cbrain_studio_architecture.md §3.4): 표시 경로는 저장 경로를 절대 건드리지 않는다.**
여기 들어오는 블록은 이미 raw 로 저장된 사본이며, 필터/다운샘플은 화면에만 적용된다.

LED 는 현재 프로토콜에 상태 비트가 없어 **호스트가 펌웨어와 같은 공식으로 재현**한다(estimated).
헤드스테이지 config(LED 밴드/임계값)를 알려주면(set_led_rules) 계산하고, 없으면 LED 레인은 비운다.
"""
from __future__ import annotations

import threading

import numpy as np

from cbrain_studio.app.detection import band_power
from cbrain_studio.core.packet import led_state


def _pow2_floor(w: int) -> int:
    n = 32
    while n * 2 <= w and n * 2 <= 512:
        n *= 2
    return n


class LedRule:
    """LED 하나의 재현 규칙 (fw_config.LedConfig 에서 옮겨옴)."""
    def __init__(self, enabled, channel, band_lo, band_hi, window_ms, threshold):
        self.enabled = bool(enabled)
        self.channel = int(channel)          # 물리 채널 (order 상의 값)
        self.band_lo = float(band_lo)
        self.band_hi = float(band_hi)
        self.window_ms = int(window_ms)
        self.threshold = float(threshold)


class DisplayBuffer:
    """기기 하나의 표시용 링버퍼. 채널×샘플을 최대 max_seconds 만큼 유지.

    쓰기: reader 스레드에서 push(block). 읽기: GUI 스레드에서 snapshot(...).
    numpy 링버퍼 + 락(짧게). 다운샘플은 읽을 때 (표시 픽셀수 기준) 수행.
    """

    def __init__(self, sr_hz: int = 1024, max_seconds: float = 60.0):
        self.sr_hz = sr_hz
        self.max_seconds = max_seconds
        self._cap = max(1, int(sr_hz * max_seconds))
        self._ch = 0
        self._order: list[int] = []
        self._buf = np.zeros((0, self._cap), dtype=np.float32)
        self._n = 0                     # 유효 샘플 수 (<= cap)
        self._head = 0                  # 다음 쓸 위치
        self._lock = threading.Lock()
        self._led_rules: list[LedRule] = []
        self._led_hist_len = int(sr_hz * max_seconds)
        self._led = np.zeros((0, 0), dtype=bool)   # (n_leds, hist)
        self._led_real = False                     # 기기 실측 LED(플래그) 수신 중이면 True(재현 대신 사용)

    def set_channels(self, ch_count: int, order: list[int]) -> None:
        with self._lock:
            self._set_channels_locked(ch_count, order)

    def _set_channels_locked(self, ch_count: int, order: list[int]) -> None:
        self._ch = ch_count
        self._order = list(order)
        self._buf = np.zeros((ch_count, self._cap), dtype=np.float32)
        self._n = 0
        self._head = 0
        n_led = self._led.shape[0] if self._led_real else len(self._led_rules)
        self._led = np.zeros((n_led, self._cap), dtype=bool)

    def set_led_rules(self, rules: list[LedRule]) -> None:
        with self._lock:
            self._led_rules = [r for r in rules if r.enabled and r.threshold > 0]
            self._led = np.zeros((len(self._led_rules), self._cap), dtype=bool)

    # ── 쓰기 (reader 스레드) ──  * 락 안에서는 _locked 헬퍼만 호출 (재진입 데드락 방지)
    def push(self, block) -> None:
        ch, spc = block.channel_count, block.n_samples
        with self._lock:
            if ch != self._ch:
                self._set_channels_locked(ch, block.order)
            data = block.channels.astype(np.float32)      # (ch, spc), raw LSB
            self._write(self._buf, data, spc)
            real = led_state(int(getattr(block, "flags", 0) or 0))
            if real is not None:
                # 기기 실측 LED (DATA flags). 블록 구간을 그 상태로 채운다(호스트 재현보다 우선).
                if not self._led_real or self._led.shape[0] != len(real):
                    self._led_real = True
                    self._led = np.zeros((len(real), self._cap), dtype=bool)
                led_col = np.array(real, dtype=bool)[:, None].repeat(spc, axis=1)
                self._write(self._led, led_col, spc)
            elif self._led_rules and not self._led_real:
                led_col = self._eval_leds_locked(block)   # (n_leds, spc) 호스트 재현(추정)
                self._write(self._led, led_col, spc)
            self._head = (self._head + spc) % self._cap
            self._n = min(self._n + spc, self._cap)

    def _write(self, buf, data, spc):
        if spc >= self._cap:
            buf[:, :] = data[:, -self._cap:]
            return
        end = self._head + spc
        if end <= self._cap:
            buf[:, self._head:end] = data
        else:
            k = self._cap - self._head
            buf[:, self._head:] = data[:, :k]
            buf[:, :end - self._cap] = data[:, k:]

    def _eval_leds_locked(self, block):
        """블록 단위로 LED on/off 를 1회 재현해 그 블록 전체를 채운다(표시용이라 충분).
        펌웨어와 동일한 band_power > threshold. window 는 링버퍼 최근 데이터 사용.
        (호출자가 이미 self._lock 을 잡고 있음 → _recent_locked 사용, 재락 금지.)"""
        n_leds = len(self._led_rules)
        out = np.zeros((n_leds, block.n_samples), dtype=bool)
        recent = self._recent_locked(int(self.sr_hz * 0.5))    # 최근 0.5s 로 대역파워
        if recent is None:
            return out
        for i, r in enumerate(self._led_rules):
            col = self._order.index(r.channel) if r.channel in self._order else r.channel
            if col >= recent.shape[0]:
                continue
            n = _pow2_floor(int(self.sr_hz * r.window_ms / 1000))
            x = recent[col, -n:]
            if x.shape[0] < n:
                continue
            p = band_power(x, self.sr_hz, r.band_lo, r.band_hi)
            out[i, :] = p > r.threshold
        return out

    def _recent_locked(self, n):
        """최근 n 샘플 (락은 호출자가 보유). eval_leds 에서는 아직 head 미갱신 상태로 호출된다."""
        if self._n == 0:
            return None
        n = min(n, self._n)
        idx = (self._head - n) % self._cap
        if idx + n <= self._cap:
            return self._buf[:, idx:idx + n]
        return np.concatenate([self._buf[:, idx:], self._buf[:, :idx + n - self._cap]], axis=1)

    # ── 읽기 (GUI 스레드) ──
    def snapshot(self, seconds: float, px: int, uv_per_lsb: float = 0.195,
                 disp_filter=None):
        """최근 seconds 를 px 폭으로 다운샘플해 반환.

        반환: dict(ch=n, t=array(px), lo/hi=(ch,px) uV, led=(n_leds,px) bool, order=list) 또는 None.
        다운샘플은 **픽셀당 min/max** (peak-preserving)."""
        with self._lock:
            if self._ch == 0 or self._n == 0:
                return None
            n = min(int(self.sr_hz * seconds), self._n)
            raw = self._recent_locked(n).copy()            # snapshot owns its data before releasing lock
            led = None
            if self._led.shape[0]:
                idx = (self._head - n) % self._cap
                if idx + n <= self._cap:
                    led = self._led[:, idx:idx + n].copy()
                else:
                    led = np.concatenate([self._led[:, idx:],
                                          self._led[:, :idx + n - self._cap]], axis=1)
            order = list(self._order)
            ch = self._ch
            led_real = self._led_real
        sig = raw.astype(np.float32)
        if disp_filter is not None:
            sig = disp_filter(sig, self.sr_hz)             # 표시 전용 필터 (사본에만)
        sig = sig * uv_per_lsb                             # → µV
        lo, hi = _minmax_downsample(sig, px)
        t = np.linspace(-n / self.sr_hz, 0, px, dtype=np.float32)
        led_ds = _any_downsample(led, px) if led is not None else np.zeros((0, px), bool)
        return {"ch": ch, "t": t, "lo": lo, "hi": hi, "led": led_ds, "order": order,
                "led_real": led_real}


def _minmax_downsample(sig: np.ndarray, px: int):
    """(ch, n) → (ch, px) 의 min/hi 두 배열 (픽셀 구간의 최소/최대)."""
    ch, n = sig.shape
    px = max(1, min(px, n))
    edges = np.linspace(0, n, px + 1, dtype=int)
    lo = np.empty((ch, px), np.float32); hi = np.empty((ch, px), np.float32)
    for i in range(px):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        seg = sig[:, a:b]
        lo[:, i] = seg.min(axis=1); hi[:, i] = seg.max(axis=1)
    return lo, hi


def _any_downsample(led: np.ndarray, px: int):
    """(k, n) bool → (k, px) : 픽셀 구간에 하나라도 True 면 True."""
    k, n = led.shape
    if n == 0:
        return np.zeros((k, px), bool)
    px = max(1, min(px, n))
    edges = np.linspace(0, n, px + 1, dtype=int)
    out = np.zeros((k, px), bool)
    for i in range(px):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        out[:, i] = led[:, a:b].any(axis=1)
    return out


# ── Display filter (display-only — never touches saved raw) ──
def make_display_filter(band=None, notch=None):
    """Compose a display-only filter: optional notch (Hz) then optional band-pass.

    band:  (lo_hz, hi_hz) or None (wideband).
    notch: 50.0 / 60.0 (Hz) or None.
    Returns a (ch, n) -> (ch, n) callback, or None if nothing selected / scipy absent.
    """
    if not band and not notch:
        return None
    try:
        from scipy.signal import butter, iirnotch, sosfiltfilt, tf2sos
    except Exception:  # noqa: BLE001
        return None

    def f(sig, sr):
        out = sig
        if notch:
            b, a = iirnotch(float(notch), 30, sr)
            out = sosfiltfilt(tf2sos(b, a), out, axis=1)
        if band:
            lo, hi = band
            sos = butter(2, [float(lo), float(hi)], btype="band", fs=sr, output="sos")
            out = sosfiltfilt(sos, out, axis=1)
        return out.astype(np.float32)

    return f
