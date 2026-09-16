"""
HAL 추상 인터페이스 — L2–L4 는 오직 이 인터페이스에만 의존한다.

구체 드라이버/트랜스포트/LED 수/동기 센서를 상위 계층이 절대 참조하지 않는다.
새 CBRAIN 세대 = 이 인터페이스들의 새 구현일 뿐, 애플리케이션 변경이 아니다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from cbrain_studio.core.types import DeviceCapabilities, SyncEvent

RawFrameHandler = Callable[[bytes], None]
SyncHandler = Callable[[SyncEvent], None]


class Transport(ABC):
    """링크 계층 — 프레임 바이트 송수신. (BLE, USB 동글, Simulated)"""

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def send(self, frame: bytes) -> None:
        """커맨드 프레임 송신 (host → device)."""

    @abstractmethod
    def set_frame_handler(self, handler: RawFrameHandler) -> None:
        """수신 원시 프레임 콜백 등록 (device → host)."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...


class Device(ABC):
    """논리적 헤드스테이지 — 정체성, 펌웨어, 배터리, 능력 서술자."""

    @property
    @abstractmethod
    def device_id(self) -> int: ...

    @abstractmethod
    async def capabilities(self) -> DeviceCapabilities:
        """능력(채널 수·LED 수·동기입력·샘플레이트)을 '읽는다' — 가정하지 않는다."""

    @abstractmethod
    async def start_stream(self) -> None: ...

    @abstractmethod
    async def stop_stream(self) -> None: ...


class LedController(ABC):
    """추상 N-LED 제어. 개수는 데이터(능력서술자), 가정이 아니다."""

    @property
    @abstractmethod
    def led_count(self) -> int: ...

    @abstractmethod
    async def set_led(self, index: int, rgb_mask: int) -> None: ...


class SyncSource(ABC):
    """제네릭 동기 입력. IR = TTL-유사 엣지/펄스. TTL/net/GPS 도 같은 인터페이스."""

    @property
    @abstractmethod
    def source_id(self) -> str: ...

    @abstractmethod
    def set_handler(self, handler: SyncHandler) -> None: ...
