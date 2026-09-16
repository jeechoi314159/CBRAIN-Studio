"""CRC16-CCITT(F): poly=0x1021, init=0xFFFF, no reflect, xorout=0. (CB v1/v2 공통)"""
from __future__ import annotations

_TABLE: list[int] = []


def _build_table() -> None:
    for b in range(256):
        crc = b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
        _TABLE.append(crc)


_build_table()


def crc16_ccitt_f(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc
