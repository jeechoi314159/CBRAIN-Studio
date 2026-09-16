"""
Acquisition Manager — 무손실 데이터 경로의 단일 소유자.

Transport(raw bytes) → Decoder(CB v2) → SampleBus → {Recorder, 그 외 소비자}.
카운터 연속성을 TimeBase 로 검사하고, 갭은 discontinuity 마커로 기록한다
(조용한 손실 금지). 시각화 등 LOSSY 소비자는 버스 하류에서 자기 뷰만 손실.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from cbrain_studio.core import packet
from cbrain_studio.core.sample_bus import SampleBus
from cbrain_studio.core.time_base import TimeBase
from cbrain_studio.core.types import SampleBlock, SyncEvent
from cbrain_studio.hal.interfaces import Transport


class AcquisitionManager:
    def __init__(self, transport: Transport, bus: SampleBus | None = None):
        self.transport = transport
        self.bus = bus or SampleBus()
        self.decoder = packet.Decoder()
        self._timebases: dict[int, TimeBase] = {}
        self._sr: dict[int, int] = {}
        self.running = False
        self.blocks_out = 0
        self.gaps = 0
        self.capabilities: packet.Capabilities | None = None   # 마지막 GET_CAPS 결과
        # 콜백: 상위(기록 등)가 등록
        self.on_discontinuity: Callable[[int, int, float, int], None] | None = None
        self.on_sync: Callable[[SyncEvent], None] | None = None
        self.on_reply: Callable[[packet.ReplyFrame], None] | None = None
        self.on_status: Callable[[packet.StatusFrame], None] | None = None
        self.on_capabilities: Callable[[packet.Capabilities], None] | None = None
        self._clock = time.monotonic

    def start(self) -> None:
        self.running = True
        self.transport.set_frame_handler(self._on_bytes)

    def stop(self) -> None:
        self.running = False

    # ── 원시 바이트 수신 ──
    def _on_bytes(self, chunk: bytes) -> None:
        if not self.running:
            return
        res = self.decoder.decode(chunk)
        now = self._clock()
        for blk in res.blocks:
            blk.decode_time = now
            self._handle_block(blk, now)
        for s in res.syncs:
            if self.on_sync:
                self.on_sync(SyncEvent(str(s.source_id), s.sample_idx, now, s.edge, s.seq_sync))
        for st in res.statuses:
            if self.on_status:
                self.on_status(st)
        for rp in res.replies:
            self._handle_reply(rp)

    def _handle_reply(self, rp: packet.ReplyFrame) -> None:
        if rp.opcode == packet.CMD_GET_CAPS and rp.status == 0:
            caps = packet.parse_capabilities(rp.data)
            if caps is not None:
                self.capabilities = caps
                if self.on_capabilities:
                    self.on_capabilities(caps)
        if self.on_reply:
            self.on_reply(rp)

    async def send_command(self, payload: bytes) -> None:
        """제어 커맨드 전송 (bare [opcode][args], §8.1). 호출은 async 컨텍스트에서."""
        await self.transport.send(payload)

    def _handle_block(self, blk: SampleBlock, now: float) -> None:
        tb = self._timebases.get(blk.device_id)
        if tb is None:
            tb = TimeBase(sr_hz=blk.sr_hz, session_epoch=time.time())
            self._timebases[blk.device_id] = tb
            self._sr[blk.device_id] = blk.sr_hz

        gap = tb.observe(blk.first_counter, blk.n_samples)
        if gap is not None:
            self.gaps += 1
            if self.on_discontinuity:
                self.on_discontinuity(blk.device_id, gap.got_counter, now, gap.missing_samples)

        self.blocks_out += 1
        self.bus.publish(blk)   # 무손실: 버스는 모든 블록을 전달

    def timebase(self, device_id: int) -> TimeBase | None:
        return self._timebases.get(device_id)
