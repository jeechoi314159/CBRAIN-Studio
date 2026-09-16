"""
코어 도메인 타입 — 채널 수에 무관(1–16, 런타임 결정).

어떤 모듈도 채널 수를 하드코딩하지 않는다. SampleBlock 은 (channels × samples)
배열을 런타임 차원으로 담으며, 획득·기록·처리·시각화가 모두 이 값을 읽어 적응한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class DeviceCapabilities:
    """디바이스 능력 서술자 — 애플리케이션은 이 값을 '읽고', 가정하지 않는다."""
    device_id: int
    firmware_version: str = "?"
    channel_count: int = 4          # 활성 채널 (1–16)
    max_channels: int = 16
    led_count: int = 2              # 하드웨어 의존 (구형 4, 현행 2)
    supported_sample_rates: tuple[int, ...] = (256, 512, 1024, 2048)
    sync_inputs: tuple[str, ...] = ("ir",)   # 제네릭 동기 소스 식별자
    tick_hz: int = 32768

    def validate_channels(self, n: int) -> int:
        return max(1, min(int(n), self.max_channels))


@dataclass
class SampleBlock:
    """한 디바이스의 연속 샘플 구간."""
    device_id: int
    first_counter: int              # 32-bit 샘플 카운터 (구간 첫 샘플)
    sr_hz: int
    channels: np.ndarray            # int16, shape (channel_count, n_samples)
    order: list[int]                # 물리 채널 ID (len == channel_count)
    decode_time: float = 0.0        # 호스트 monotonic 도착 시각(초)
    flags: int = 0
    enc: int = 0                    # 샘플 인코딩: 0=i16 2's-comp, 1=offset-binary(legacy ±5mV)

    @property
    def channel_count(self) -> int:
        return self.channels.shape[0]

    @property
    def n_samples(self) -> int:
        return self.channels.shape[1]

    @property
    def last_counter(self) -> int:
        return (self.first_counter + self.n_samples - 1) & 0xFFFFFFFF


# ADC → 전압 변환. 풀스케일 ±5 mV over 15-bit+sign → µV/LSB = 5000/32768.
ADC_MIDSCALE = 32767
ADC_FULLSCALE_MV = 5.0
UV_PER_LSB = ADC_FULLSCALE_MV * 1000.0 / 32768.0   # ≈ 0.15259 µV/LSB


def raw_to_millivolts(raw: np.ndarray) -> np.ndarray:
    """레거시 offset-binary(enc=1) 앵커: (raw-32767)*5/32768 → ±5 mV."""
    return (raw.astype(np.float64) - ADC_MIDSCALE) * (ADC_FULLSCALE_MV / 32768.0)


def raw_to_microvolts(raw: np.ndarray, enc: int = 0) -> np.ndarray:
    """ADC 원값 → µV. enc=0(2's-comp, 0 중심): raw*µV/LSB. enc=1(offset-binary): (raw-32767)*…"""
    x = raw.astype(np.float64)
    if enc == 1:
        x = x - ADC_MIDSCALE
    return x * UV_PER_LSB


@dataclass
class DeviceStatus:
    device_id: int
    battery_pct: int | None = None
    battery_mv: int | None = None
    link_rssi: int | None = None
    led_state: list[int] = field(default_factory=list)   # LED index → RGB mask
    firmware_version: str = "?"
    host_time: float = 0.0


@dataclass
class EventMarker:
    """타임스탬프가 붙은 검출 이벤트/명령 (예: LED 트리거)."""
    device_id: int
    counter: int
    host_time: float
    code: int
    value: int = 0
    label: str = ""


@dataclass
class SyncEvent:
    """제네릭 SyncSource 로부터의 동기 펄스 (IR=TTL-유사)."""
    source: str                     # "ir", "ttl", "net", "gps" …
    counter: int | None             # 대응 디바이스 샘플 카운터(있으면)
    host_time: float
    edge: int = 1                   # 1=rising, 0=falling
    value: int = 0
