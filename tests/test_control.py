"""Control/Reply 경로 (A2) — 하드웨어 없이 데스크톱 절반 검증.

펌웨어(cb_proto.h cb_encode_caps)와 §8.4 레이아웃이 어긋나지 않는지, 능력서술자가
AcquisitionManager.capabilities 로 올라오는지, 커맨드 인코딩이 opcode 계약을 지키는지.
"""
from __future__ import annotations

import struct

from cbrain_studio.app.acquisition import AcquisitionManager
from cbrain_studio.core import packet
from cbrain_studio.hal.simulated import SimulatedTransport


def _firmware_caps_descriptor() -> bytes:
    """cb_encode_caps() 가 만드는 §8.4 서술자 바이트를 손수 재현(필드순서 크로스체크)."""
    d = bytes([
        2,            # proto_ver
        0, 1, 0,      # fw major/minor/patch
        1,            # hw_rev
        16,           # max_channels (RHD2216 physical)
        2,            # led_count
        0x01,         # sync_inputs (bit0 IR)
        0,            # enc = i16
    ])
    d += struct.pack("<f", 5000.0 / 32768.0)   # uv_per_lsb
    d += bytes([1]) + struct.pack("<H", 1024)  # n_rates, rates[]
    return d


def test_caps_reply_surfaces_capabilities():
    """GET_CAPS REPLY 프레임 → AcquisitionManager.capabilities 로 파싱되어 올라온다."""
    acq = AcquisitionManager(SimulatedTransport(device_id=1))
    got = []
    acq.on_capabilities = got.append
    acq.start()

    frame = packet.encode_reply(7, packet.CMD_GET_CAPS, 0x00, _firmware_caps_descriptor())
    acq._on_bytes(frame)

    caps = acq.capabilities
    assert caps is not None and got and got[0] is caps
    assert caps.proto_ver == 2
    assert caps.fw == (0, 1, 0)
    assert caps.max_channels == 16
    assert caps.led_count == 2
    assert caps.sync_inputs == 0x01
    assert caps.rates == [1024]
    assert abs(caps.uv_per_lsb - 5000.0 / 32768.0) < 1e-6


def test_generic_reply_dispatched():
    """START/STOP 같은 일반 REPLY 는 on_reply 로 그대로 전달(status 포함)."""
    acq = AcquisitionManager(SimulatedTransport(device_id=1))
    seen = []
    acq.on_reply = seen.append
    acq.start()

    acq._on_bytes(packet.encode_reply(1, packet.CMD_STOP_STREAM, 0x00))
    acq._on_bytes(packet.encode_reply(2, 0x99, 0xFE))     # 미지원 opcode

    assert [(r.opcode, r.status) for r in seen] == [(packet.CMD_STOP_STREAM, 0), (0x99, 0xFE)]


def test_command_encoders_match_opcodes():
    """bare 커맨드는 [opcode][args] 계약(§8.1)을 지킨다 — 펌웨어 switch 와 일치."""
    assert packet.cmd_start_stream() == bytes([0x24])
    assert packet.cmd_stop_stream() == bytes([0x25])
    assert packet.cmd_get_caps() == bytes([0x31])
    assert packet.cmd_get_fw_version() == bytes([0x42])
    assert packet.cmd_set_led(1, 255, 0, 0) == bytes([0x50, 1, 255, 0, 0])


def test_set_chmap_encoding():
    """SET_CHMAP (§8.1): [ch_map u16 LE][ord_len][ord…]. int N → 0..N-1."""
    import struct
    # N=8 → ch_map=0x00FF, ord=[0..7]
    assert packet.cmd_set_chmap(8) == bytes([0x20]) + struct.pack("<H", 0x00FF) + bytes([8]) + bytes(range(8))
    # 명시 리스트 [2,5] → ch_map = (1<<2)|(1<<5) = 0x24
    assert packet.cmd_set_chmap([2, 5]) == bytes([0x20]) + struct.pack("<H", 0x24) + bytes([2, 2, 5])
