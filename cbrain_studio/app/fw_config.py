"""
CB 펌웨어 설정 blob (ConfigStruct) — GUI(생성) ↔ 펌웨어(소비) 바이너리 계약.

범용 `base.hex` 는 부팅 시 플래시 예약 페이지(CB_CONFIG_ADDR)에서 이 blob 을 읽어
측정 조건과 2개 LED 밴드파워 폐루프 규칙대로 자율 동작한다(캘리브레이션은 GUI 측정
절대 임계, docs/firmware_configurator.md §7). 모든 정수 little-endian, 고정 크기.

레이아웃:
  header(8): magic "CBCF" · version u16 · crc16 u16 (body 대상, CRC16-CCITT(F))
  body:
    measurement(24): sr_hz u16 · notch u8 · bw_preset u8 · ch_count u8 · flags u8
                     · pad u16 · ch_map u8[16](0xFF=미사용)
    led[2](20 each): enabled u8 · channel u8 · window_ms u16 · band_lo f32 · band_hi f32
                     · threshold f32 · r,g,b,intensity u8×4

LED 게이트는 **샘플 단위**: 매 샘플마다 직전 window_ms 구간의 대역파워가 threshold 를
넘으면 ON, 아니면 OFF (hold/히스테리시스/패턴 없음).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from cbrain_studio.core.crc import crc16_ccitt_f

MAGIC = b"CBCF"
VERSION = 1
CB_CONFIG_ADDR = 0x70000        # 앱 위 · storage 파티션(0x7a000+) 아래 자유영역 — 펌웨어와 공유

_HDR = "<4sHH"                  # magic, version, crc
_MEAS = "<HBBBBH16s"            # sr, notch, bw_preset, ch_count, flags, pad, ch_map
_LED = "<BBHfffBBBB"           # enabled, channel, window_ms, lo, hi, threshold, r,g,b,intensity
_MEAS_SIZE = struct.calcsize(_MEAS)   # 24
_LED_SIZE = struct.calcsize(_LED)     # 20
BODY_SIZE = _MEAS_SIZE + 2 * _LED_SIZE
BLOB_SIZE = struct.calcsize(_HDR) + BODY_SIZE


@dataclass
class LedConfig:
    enabled: bool = False
    channel: int = 0
    window_ms: int = 250            # 직전 이 구간을 FFT (샘플 단위 게이트)
    band_lo: float = 8.0            # 예: alpha 8–12 Hz
    band_hi: float = 12.0
    threshold: float = 0.0          # GUI 측정 절대 대역파워 임계
    color: tuple[int, int, int] = (255, 0, 0)
    intensity: int = 100            # 0–100 (%)

    def pack(self) -> bytes:
        r, g, b = self.color
        return struct.pack(_LED, int(self.enabled), self.channel & 0xFF, self.window_ms,
                           float(self.band_lo), float(self.band_hi), float(self.threshold),
                           r & 0xFF, g & 0xFF, b & 0xFF, max(0, min(100, self.intensity)))

    @classmethod
    def unpack(cls, buf: bytes, off: int) -> "LedConfig":
        en, ch, win, lo, hi, thr, r, g, b, inten = struct.unpack_from(_LED, buf, off)
        return cls(bool(en), ch, win, lo, hi, thr, (r, g, b), inten)


@dataclass
class FwConfig:
    sr_hz: int = 1024
    notch: int = 60                 # 0=off, 50, 60
    bw_preset: int = 0              # 0 = 0.1–250 Hz (기본)
    ch_count: int = 4
    stream_ble: bool = True         # flags bit0
    ch_map: list[int] = field(default_factory=lambda: [0, 1, 2, 3])
    leds: list[LedConfig] = field(default_factory=lambda: [LedConfig(), LedConfig()])

    def to_bytes(self) -> bytes:
        flags = 0x01 if self.stream_ble else 0x00
        chmap = bytes((list(self.ch_map)[:16] + [0xFF] * 16)[:16])
        meas = struct.pack(_MEAS, self.sr_hz, self.notch & 0xFF, self.bw_preset & 0xFF,
                           self.ch_count & 0xFF, flags, 0, chmap)
        leds = (self.leds + [LedConfig(), LedConfig()])[:2]
        body = meas + leds[0].pack() + leds[1].pack()
        crc = crc16_ccitt_f(body)
        return struct.pack(_HDR, MAGIC, VERSION, crc) + body

    @classmethod
    def from_bytes(cls, data: bytes) -> "FwConfig":
        magic, ver, crc = struct.unpack_from(_HDR, data, 0)
        if magic != MAGIC:
            raise ValueError(f"bad magic {magic!r}")
        if ver != VERSION:
            raise ValueError(f"unsupported version {ver}")
        body = data[struct.calcsize(_HDR):struct.calcsize(_HDR) + BODY_SIZE]
        if crc16_ccitt_f(body) != crc:
            raise ValueError("crc mismatch")
        sr, notch, bw, chc, flags, _pad, chmap = struct.unpack_from(_MEAS, body, 0)
        ch_map = [c for c in chmap[:chc] if c != 0xFF]
        off = _MEAS_SIZE
        leds = [LedConfig.unpack(body, off), LedConfig.unpack(body, off + _LED_SIZE)]
        return cls(sr, notch, bw, chc, bool(flags & 0x01), ch_map, leds)
