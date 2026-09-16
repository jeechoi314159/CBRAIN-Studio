"""
샘플 버스 — 디코드된 샘플의 단일 정렬 스트림을 소비자에게 fan-out.

디바이스별로 키가 나뉘며(멀티 디바이스 대비), 소비자는 콜백으로 구독한다.
소비자 유형:
  - LOSSLESS(기록): 예외 없이 모든 블록을 받는다. 여기서 버리지 않는다.
  - LOSSY(시각화 등): 자체 링버퍼로 자기 뷰만 손실 허용(버스는 그대로 전달).

버스 자체는 블록을 버리지 않는다 — 무손실 불변식(acquisition→recording)을 지킨다.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from .types import SampleBlock

Consumer = Callable[[SampleBlock], None]


class SampleBus:
    def __init__(self) -> None:
        self._consumers: list[Consumer] = []
        self._lock = threading.Lock()
        self.published = 0

    def subscribe(self, consumer: Consumer) -> None:
        with self._lock:
            self._consumers.append(consumer)

    def unsubscribe(self, consumer: Consumer) -> None:
        with self._lock:
            if consumer in self._consumers:
                self._consumers.remove(consumer)

    def publish(self, block: SampleBlock) -> None:
        with self._lock:
            consumers = list(self._consumers)
        self.published += 1
        for c in consumers:
            c(block)   # 소비자 예외는 각자 책임(기록은 절대 예외 삼키지 않음)
