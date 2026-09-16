"""Profile 저장/불러오기 — 사전 설정 영속화."""
from __future__ import annotations

from cbrain_studio.app.profile import Profile


def test_roundtrip_json(tmp_path):
    p = Profile(name="alpha_gate", calib_k=2.5)
    p.device.name_prefix = "CBRAIN"
    p.channels.count = 8
    p.channels.order = list(range(8))
    p.freq_to_lit.band_lo_hz = 8.0
    p.freq_to_lit.band_hi_hz = 12.0
    p.display.y_autoscale = False
    p.display.y_range_uv = 150.0

    path = p.save(str(tmp_path / "alpha_gate.json"))
    q = Profile.load(path)

    assert q.name == "alpha_gate" and q.calib_k == 2.5
    assert q.channels.count == 8 and q.channels.order == list(range(8))
    assert q.freq_to_lit.band_lo_hz == 8.0 and q.freq_to_lit.band_hi_hz == 12.0
    assert q.display.y_autoscale is False and q.display.y_range_uv == 150.0


def test_from_dict_ignores_unknown_fields():
    """전방호환: 미래 필드/오타는 무시하고 기본값 유지."""
    p = Profile.from_dict({"name": "x", "future_field": 123,
                           "display": {"y_range_uv": 300.0, "bogus": 1}})
    assert p.name == "x"
    assert p.display.y_range_uv == 300.0
    assert p.display.y_autoscale is True          # 기본값 보존


def test_defaults_are_sane():
    p = Profile()
    assert p.channels.count == 4
    assert p.freq_to_lit.enabled and p.freq_to_lit.window_ms == 250
    assert p.display.mode == "wave"
