"""펌웨어 설정 blob 계약 — 라운드트립·CRC·크기 검증 (펌웨어 C 와 바이트 호환)."""
from __future__ import annotations

import struct

import pytest

from cbrain_studio.app.fw_config import (
    BLOB_SIZE, FwConfig, LedConfig, MAGIC, VERSION,
)


def test_roundtrip():
    cfg = FwConfig(
        sr_hz=512, notch=50, ch_count=8, stream_ble=False, ch_map=list(range(8)),
        leds=[
            LedConfig(True, channel=0, band_lo=8, band_hi=12, threshold=1234.5,
                      color=(0, 255, 0), intensity=80),
            LedConfig(True, channel=3, band_lo=30, band_hi=80, threshold=99.0,
                      color=(0, 0, 255), intensity=50),
        ],
    )
    blob = cfg.to_bytes()
    assert len(blob) == BLOB_SIZE
    assert blob[:4] == MAGIC

    got = FwConfig.from_bytes(blob)
    assert got.sr_hz == 512 and got.notch == 50 and got.ch_count == 8
    assert got.stream_ble is False and got.ch_map == list(range(8))
    assert got.leds[0].enabled and got.leds[0].color == (0, 255, 0)
    assert abs(got.leds[0].threshold - 1234.5) < 1e-3 and got.leds[0].intensity == 80
    assert got.leds[1].channel == 3 and got.leds[1].band_hi == 80


def test_crc_detects_corruption():
    blob = bytearray(FwConfig().to_bytes())
    blob[20] ^= 0xFF                         # body 손상
    with pytest.raises(ValueError, match="crc"):
        FwConfig.from_bytes(bytes(blob))


def test_bad_magic_and_version():
    with pytest.raises(ValueError, match="magic"):
        FwConfig.from_bytes(b"XXXX" + b"\x00" * (BLOB_SIZE - 4))
    blob = bytearray(FwConfig().to_bytes())
    struct.pack_into("<H", blob, 4, VERSION + 9)    # version 바꿈(crc는 body만 → magic/ver 체크 별개)
    with pytest.raises(ValueError, match="version"):
        FwConfig.from_bytes(bytes(blob))


def test_defaults_layout_stable():
    # 고정 크기(72B): header8 + meas24 + led20×2
    assert BLOB_SIZE == 8 + 24 + 20 * 2 == 72
    assert len(FwConfig().to_bytes()) == 72
