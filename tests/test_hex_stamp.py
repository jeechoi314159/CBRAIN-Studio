"""HexStamper — 설정 blob 을 hex 에 찍고 되읽어 라운드트립 검증."""
from __future__ import annotations

from cbrain_studio.app.fw_config import CB_CONFIG_ADDR, FwConfig, LedConfig
from cbrain_studio.app.hex_stamp import emit_hex, parse_hex, read_region, stamp_config


def _tiny_base_hex() -> str:
    # 앱 코드가 저주소에 조금 있는 base.hex 흉내
    mem = {i: (i & 0xFF) for i in range(0x0000, 0x0040)}
    mem.update({0x10000 + i: 0xAA for i in range(4)})   # ELA 경계 넘는 데이터
    return emit_hex(mem, start_addr=0x0000)


def test_parse_emit_roundtrip():
    text = _tiny_base_hex()
    mem, start = parse_hex(text)
    text2 = emit_hex(mem, start)
    mem2, start2 = parse_hex(text2)
    assert mem == mem2 and start == start2


def test_stamp_and_read_back_config():
    base = _tiny_base_hex()
    cfg = FwConfig(sr_hz=512, notch=50, ch_count=2, ch_map=[0, 1],
                   leds=[LedConfig(True, channel=0, band_lo=8, band_hi=12, threshold=777.0,
                                   color=(0, 255, 0), intensity=70), LedConfig()])
    blob = cfg.to_bytes()
    stamped = stamp_config(base, blob, CB_CONFIG_ADDR)

    # 원래 앱 데이터 보존
    mem, _ = parse_hex(stamped)
    assert mem[0x0000] == 0x00 and mem[0x0010] == 0x10 and mem[0x10000] == 0xAA
    # 찍힌 설정 되읽기
    region = read_region(stamped, CB_CONFIG_ADDR, len(blob))
    got = FwConfig.from_bytes(region)
    assert got.sr_hz == 512 and got.ch_count == 2
    assert got.leds[0].threshold == 777.0 and got.leds[0].color == (0, 255, 0)


def test_stamp_into_real_firmware_hex(tmp_path):
    import os
    hexp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "firmware/cb_intan/build/cb_intan/zephyr/zephyr.hex")
    if not os.path.exists(hexp):
        import pytest
        pytest.skip("firmware hex 없음 (빌드 필요)")
    with open(hexp) as f:
        base = f.read()
    blob = FwConfig().to_bytes()
    stamped = stamp_config(base, blob, CB_CONFIG_ADDR)
    assert FwConfig.from_bytes(read_region(stamped, CB_CONFIG_ADDR, len(blob))).sr_hz == 1024
    # 앱 전체(저·고주소 모두)가 그대로 남아야 — type02 세그먼트 주소 회귀 방지
    base_mem, _ = parse_hex(base)
    stamped_mem, _ = parse_hex(stamped)
    assert max(base_mem) > 0x30000, "base hex 가 64KB 넘게 있어야 회귀 테스트 의미"
    for a in base_mem:                       # 모든 앱 바이트 보존
        assert stamped_mem[a] == base_mem[a]
