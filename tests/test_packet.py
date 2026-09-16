"""CB v2 코덱 — 라운드트립, 조각화, CRC, 채널 가변(1–16) 테스트."""
from __future__ import annotations

import numpy as np

from cbrain_studio.core import packet


def _sig(ch, spc, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(-3000, 3000, size=(ch, spc), dtype=np.int16)


def test_roundtrip_basic():
    sig = _sig(4, 32)
    frame = packet.encode_data(1, device_id=7, channels=sig, first_counter=100,
                               sr_hz=1024, order=[0, 1, 2, 3])
    dec = packet.Decoder()
    res = dec.decode(frame)
    assert len(res.blocks) == 1 and dec.frames_ok == 1 and dec.frames_bad == 0
    b = res.blocks[0]
    assert b.device_id == 7 and b.sr_hz == 1024 and b.first_counter == 100
    assert b.channel_count == 4 and b.n_samples == 32
    assert np.array_equal(b.channels, sig)
    assert b.last_counter == 131


def test_led_state_helper():
    assert packet.led_state(0) is None                                   # 구형 펌웨어: 미보고
    assert packet.led_state(packet.FLAG_LED_VALID) == [False, False]     # 둘 다 off
    assert packet.led_state(packet.FLAG_LED_VALID | packet.FLAG_LED0) == [True, False]
    assert packet.led_state(packet.FLAG_LED_VALID | packet.FLAG_LED1) == [False, True]
    # OVFL/DROPPED(bit0/1) 만 세팅 & VALID 없으면 LED 정보 아님
    assert packet.led_state(packet.FLAG_OVFL | packet.FLAG_DROPPED) is None


def test_flags_led_roundtrip():
    sig = _sig(4, 16)
    frame = packet.encode_data(1, 7, sig, 0, 1024,
                               flags=packet.FLAG_LED_VALID | packet.FLAG_LED1)
    b = packet.Decoder().decode(frame).blocks[0]
    assert packet.led_state(b.flags) == [False, True]                    # 실측 LED2 on


def test_variable_channel_count():
    for ch in (1, 2, 3, 7, 16):
        sig = _sig(ch, 20, seed=ch)
        frame = packet.encode_data(0, 1, sig, 0, 512, order=list(range(ch)))
        res = packet.Decoder().decode(frame)
        assert len(res.blocks) == 1
        assert res.blocks[0].channel_count == ch
        assert np.array_equal(res.blocks[0].channels, sig)


def test_offset_binary_encoding():
    """enc=1 (legacy offset-binary) 프레임도 디코드되어야 함(§6.4)."""
    sig = _sig(4, 16)
    frame = packet.encode_data(0, 1, sig, 0, 1024, enc=1)
    res = packet.Decoder().decode(frame)
    assert len(res.blocks) == 1 and res.blocks[0].enc == 1
    assert np.array_equal(res.blocks[0].channels, sig)


def test_fragmented_stream():
    frames = b""
    origs = []
    for s in range(6):
        sig = _sig(8, 16, seed=s)
        origs.append(sig)
        frames += packet.encode_data(s, 2, sig, s * 16, 1024, order=list(range(8)))
    dec = packet.Decoder()
    got = []
    for i in range(0, len(frames), 17):    # 17B 조각(MTU 흉내)
        got += dec.decode(frames[i:i + 17]).blocks
    assert len(got) == 6
    for b, o in zip(got, origs):
        assert np.array_equal(b.channels, o)


def test_crc_reject():
    frame = bytearray(packet.encode_data(0, 1, _sig(2, 8), 0, 1024))
    frame[-1] ^= 0xFF
    dec = packet.Decoder()
    res = dec.decode(bytes(frame))
    assert len(res.blocks) == 0 and dec.frames_bad >= 1


def test_sync_status_reply_roundtrip():
    dec = packet.Decoder()
    res = dec.decode(packet.encode_sync(1, device_id=3, source_id=1, edge=1,
                                        seq_sync=7, dev_tick=123, sample_idx=999))
    assert len(res.syncs) == 1
    s = res.syncs[0]
    assert s.device_id == 3 and s.source_id == 1 and s.sample_idx == 999 and s.seq_sync == 7

    res = dec.decode(packet.encode_status(2, device_id=3, batt_mv=3900, batt_pct=88,
                                          link_rssi=-52, led_state=[1, 4]))
    assert len(res.statuses) == 1
    st = res.statuses[0]
    assert st.batt_pct == 88 and st.link_rssi == -52 and st.led_state == [1, 4]

    res = dec.decode(packet.encode_reply(3, opcode=0x41, status=0x00, data=b"\x2c\x0f\x58"))
    assert len(res.replies) == 1
    rp = res.replies[0]
    assert rp.opcode == 0x41 and rp.status == 0x00 and rp.data == b"\x2c\x0f\x58"


def test_capabilities_roundtrip():
    caps = packet.Capabilities(proto_ver=2, fw=(2, 0, 1), hw_rev=3, max_channels=16,
                               led_count=2, sync_inputs=0b01, enc=0, uv_per_lsb=0.195,
                               rates=[256, 512, 1024, 2048])
    frame = packet.encode_capabilities(9, caps)
    res = packet.Decoder().decode(frame)
    assert len(res.replies) == 1 and res.replies[0].opcode == packet.CMD_GET_CAPS
    got = packet.parse_capabilities(res.replies[0].data)
    assert got.max_channels == 16 and got.led_count == 2 and got.rates == [256, 512, 1024, 2048]
    assert abs(got.uv_per_lsb - 0.195) < 1e-6 and got.fw == (2, 0, 1)


def test_command_encoders():
    # 표준은 CRC-framed COMMAND(type 0x10); build_command 은 저지연 bare payload
    assert packet.cmd_set_sr_hz(1024) == bytes([0x21, 0x00, 0x04])
    assert packet.cmd_set_spc(5) == bytes([0x23, 0x05])
    assert packet.cmd_set_led(1, 5, 0, 0) == bytes([0x50, 0x01, 0x05, 0x00, 0x00])
    assert packet.cmd_get_caps() == bytes([0x31])
    framed = packet.encode_command(0, packet.CMD_START_STREAM)
    res = packet.Decoder().decode(framed)   # 프레임 유효성(CRC) 확인
    assert res.blocks == [] and packet.Decoder().frames_bad == 0


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
            print("OK", fn.__name__)
    print("\n모든 packet 테스트 통과")
