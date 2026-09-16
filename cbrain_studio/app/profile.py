"""
Acquisition Profile — 실험 전 미리 결정하는 설정을 하나로 묶어 저장/불러오기.

디바이스·채널·frequency-to-lit(대역 점등)·디스플레이 설정을 JSON 으로 영속화한다.
project.md '실험 재현성 / recording profiles' 요구를 뒷받침한다. UI·기록 등 상위 블록은
이 값을 '읽어' 동작하며, 어떤 것도 하드코딩하지 않는다.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields


@dataclass
class DeviceSetup:
    name_prefix: str = "CBRAIN"     # 광고 이름 접두
    address: str | None = None      # BLE 주소 직접 지정(선택)


@dataclass
class ChannelSetup:
    count: int = 4                  # 활성 채널 수 (1–16) → SET_CHMAP
    order: list[int] = field(default_factory=lambda: [0, 1, 2, 3])   # 물리 채널 순서


@dataclass
class FrequencyToLit:
    """특정 대역 뇌파 발생을 LED 로 표시하는 폐루프 설정."""
    enabled: bool = True
    band_lo_hz: float = 8.0         # 대역 하한 (예: alpha 8–12 Hz)
    band_hi_hz: float = 12.0        # 대역 상한
    window_ms: int = 250            # FFT 윈도우 길이
    k: float = 3.0                  # threshold = baseline mean + k·SD (대역 파워)
    led_index: int = 0              # 점등할 LED
    channels: list[int] | None = None   # 감시 채널(None=전체)


@dataclass
class DisplaySetup:
    y_autoscale: bool = True        # True=오토스케일, False=절대 범위
    y_range_uv: float = 200.0       # 절대 모드일 때 ± 범위 (µV)
    x_window_ms: int = 2000         # 표시 시간 창 (ms)
    mode: str = "wave"              # "wave" | "spectrum"


@dataclass
class Profile:
    """실험 프로파일 — 사전 설정 일체."""
    name: str = "default"
    calib_k: float = 3.0            # 검출 임계 계수
    device: DeviceSetup = field(default_factory=DeviceSetup)
    channels: ChannelSetup = field(default_factory=ChannelSetup)
    freq_to_lit: FrequencyToLit = field(default_factory=FrequencyToLit)
    display: DisplaySetup = field(default_factory=DisplaySetup)

    # ── 직렬화 ──
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        d = dict(d)
        nested = {"device": DeviceSetup, "channels": ChannelSetup,
                  "freq_to_lit": FrequencyToLit, "display": DisplaySetup}
        kwargs = {}
        valid = {f.name for f in fields(cls)}
        for k, v in d.items():
            if k not in valid:
                continue                       # 미래 필드/오타는 무시(전방호환)
            if k in nested and isinstance(v, dict):
                sub = nested[k]
                sub_valid = {f.name for f in fields(sub)}
                kwargs[k] = sub(**{kk: vv for kk, vv in v.items() if kk in sub_valid})
            else:
                kwargs[k] = v
        return cls(**kwargs)

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return path

    @classmethod
    def load(cls, path: str) -> "Profile":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
