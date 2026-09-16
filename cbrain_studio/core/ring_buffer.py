"""
바운드 링버퍼 — 생산자/소비자 사이 백프레셔.

정책은 소비자별로 선택한다:
  - drop_oldest=True  : 가득 차면 가장 오래된 항목을 버림(시각화 등 '자기 뷰'만 손실).
  - drop_oldest=False : 가득 차면 push 가 False 반환(기록처럼 손실 불가 소비자 → 블록/확장 결정은 상위가).

기록 경로는 이 버퍼로 샘플을 '버리지' 않는다. 무손실 불변식은 acquisition 이 보장.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class RingBuffer(Generic[T]):
    def __init__(self, capacity: int, drop_oldest: bool = True) -> None:
        self.capacity = capacity
        self.drop_oldest = drop_oldest
        self._dq: deque[T] = deque()
        self._lock = threading.Lock()
        self.dropped = 0

    def push(self, item: T) -> bool:
        with self._lock:
            if len(self._dq) >= self.capacity:
                if self.drop_oldest:
                    self._dq.popleft()
                    self.dropped += 1
                else:
                    return False
            self._dq.append(item)
            return True

    def pop_all(self) -> list[T]:
        with self._lock:
            items = list(self._dq)
            self._dq.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._dq)
