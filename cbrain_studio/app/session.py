"""
다중 헤드스테이지 획득 세션 — DeviceLink(기기 하나의 생애주기) + SessionManager(오케스트레이션).

설계: docs/cbrain_studio_architecture.md §3. 기존 자산 재사용:
  BridgeTransport(동글당 1개) → AcquisitionManager(자기 Decoder) → SampleBus → DeviceRecorder.

핵심 원칙:
  · 동글 1개 = 독립 파이프라인 1개 (Decoder 공유 금지 — 바이트 재조립이 포트별로 독립).
  · 획득(streaming)과 기록(recording)을 분리한다.
  · target_id(지정) 와 device_id(실측 프레임값)를 분리 추적 — 불일치는 사고 신호.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from cbrain_studio.app.acquisition import AcquisitionManager
from cbrain_studio.app.recording import DeviceRecorder
from cbrain_studio.core import packet
from cbrain_studio.hal.bridge import TARGET_ANY, BridgeTransport, set_target_frame


class LinkState(Enum):
    IDLE = "idle"            # 동글은 있으나 페어링 안 됨
    PAIRING = "pairing"      # SET_TARGET 후 연결 대기 (~15-20s)
    STREAMING = "streaming"  # 데이터 수신 중 (기록 아님)
    RECORDING = "recording"  # 파일에 기록 중
    STALE = "stale"          # 2s 이상 데이터 없음 → 경보
    ERROR = "error"          # 포트/링크 오류


STALE_AFTER_S = 2.0


@dataclass
class LinkStats:
    """1초 주기로 UI/HealthMonitor 가 읽는 기기별 지표 (HealthMonitor 가 파생값 계산)."""
    blocks: int = 0
    frames_ok: int = 0
    frames_bad: int = 0
    gaps: int = 0
    missing_samples: int = 0
    gaps_baseline: int = -1          # 안정화 후 확정된 '시작 갭'(-1=미확정). recent_gaps = gaps - baseline
    settle_time: float = 0.0         # 안정화 시각(host monotonic)
    last_block_time: float = 0.0     # host monotonic (마지막 블록 도착)

    @property
    def recent_gaps(self) -> int:
        """연결 안정화 이후 발생한 진짜 갭 (초기 카운터 정렬 갭 제외)."""
        return max(0, self.gaps - self.gaps_baseline) if self.gaps_baseline >= 0 else 0
    samples_total: int = 0           # 누적 수신 샘플 (실효 fs 계산용)
    written_samples: int = 0
    sr_hz: int = 0
    channel_count: int = 0


def _run(coro):
    """BridgeTransport 의 async(실제 await 없음)를 동기 실행."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class DeviceLink:
    """동글 포트 ↔ 헤드스테이지 번호(target_id)의 1:1 결합 + 상태기계."""

    def __init__(self, port: str, target_id: int, bus_consumer=None):
        self.port = port
        self.target_id = target_id
        self.device_id: int | None = None      # 실제 수신 프레임의 device_id
        self.state = LinkState.IDLE
        self.stats = LinkStats()
        self.error = ""

        self.tx: BridgeTransport | None = None
        self.acq: AcquisitionManager | None = None
        self.rec: DeviceRecorder | None = None
        self._rec_lock = threading.Lock()
        self._want_rec = False
        self._out_dir = ""
        self._extra_consumer = bus_consumer    # 표시 등 외부 소비자 (블록 그대로 전달)

    # ── 페어링/스트리밍 ──
    def pair(self) -> None:
        """동글 열고 SET_TARGET 전송 → 데이터 오면 STREAMING 으로 전이(_on_block)."""
        self.tx = BridgeTransport(self.port)
        self.acq = AcquisitionManager(self.tx)
        self.acq.on_discontinuity = self._on_disc
        self.acq.start()
        self.acq.bus.subscribe(self._on_block)
        _run(self.tx.open())
        _run(self.tx.send(set_target_frame(self.target_id)))
        self.state = LinkState.PAIRING
        self.error = ""

    def close(self) -> None:
        self.stop_recording()
        if self.acq is not None:
            self.acq.stop()
        if self.tx is not None:
            try:
                _run(self.tx.close())
            except Exception:  # noqa: BLE001
                pass
        self.tx = self.acq = None
        self.state = LinkState.IDLE

    # ── 기록 ──
    def arm_recording(self, out_dir: str) -> None:
        """기록 예약 — 다음 블록부터 파일에 쓴다(첫 블록의 채널/sr 로 파일 open)."""
        self._out_dir = out_dir
        self._want_rec = True

    def stop_recording(self) -> str | None:
        self._want_rec = False
        with self._rec_lock:
            rec, self.rec = self.rec, None
        path = rec.close() if rec is not None else None
        if self.state == LinkState.RECORDING:
            self.state = LinkState.STREAMING
        return path

    @property
    def recording(self) -> bool:
        return self.rec is not None

    # ── reader 스레드 콜백 (블록 도착) ──
    def _on_block(self, blk):
        now = time.monotonic()
        self.device_id = blk.device_id
        s = self.stats
        s.blocks += 1
        s.last_block_time = now
        s.samples_total += blk.n_samples
        s.sr_hz = blk.sr_hz
        s.channel_count = blk.channel_count
        if self.acq is not None:
            s.frames_ok = self.acq.decoder.frames_ok
            s.frames_bad = self.acq.decoder.frames_bad
            s.gaps = self.acq.gaps

        if self.state in (LinkState.PAIRING, LinkState.STALE):
            # 첫 안정화: 이 시점의 누적 갭을 기준으로 삼는다(연결 정렬 갭은 진짜 손실이 아님).
            # 안정화 후 ~1초 지나서 확정(초기 정렬 갭이 몇 블록에 걸쳐 발생하므로).
            if self.stats.settle_time == 0.0:
                self.stats.settle_time = now
            self.state = LinkState.RECORDING if self.recording else LinkState.STREAMING
        # 안정화 후 ~1초가 지나면 그 시점의 누적 갭을 '시작 갭' 기준으로 확정(1회만).
        if (self.stats.settle_time and self.stats.gaps_baseline < 0
                and now - self.stats.settle_time > 1.0):
            self.stats.gaps_baseline = self.stats.gaps

        # 기록 (첫 매칭 블록에서 파일 open)
        with self._rec_lock:
            if self._want_rec and self.rec is None:
                os.makedirs(self._out_dir, exist_ok=True)
                self.rec = DeviceRecorder(self._out_dir, blk.device_id)
                self.rec.open(blk.channel_count, blk.sr_hz, blk.order)
                self.state = LinkState.RECORDING
            if self.rec is not None:
                tb = self.acq.timebase(blk.device_id)
                counters = list(range(blk.first_counter, blk.first_counter + blk.n_samples))
                ts = tb.sample_times(blk.first_counter, blk.n_samples)
                self.rec.write_block(blk, counters, ts)
                s.written_samples = self.rec.n_written

        if self._extra_consumer is not None:
            self._extra_consumer(blk)          # 표시 버퍼 등

    def _on_disc(self, device_id, counter, host_time, missing):
        self.stats.missing_samples += missing
        with self._rec_lock:
            if self.rec is not None:
                self.rec.add_discontinuity(counter, host_time, missing)

    # ── 상태 갱신 (HealthMonitor 가 주기 호출) ──
    def tick(self, now: float) -> None:
        if self.state in (LinkState.STREAMING, LinkState.RECORDING):
            if now - self.stats.last_block_time > STALE_AFTER_S:
                self.state = LinkState.STALE     # 경보만 — 자동 재연결 안 함(설계 결정)

    @property
    def is_wildcard(self) -> bool:
        """와일드카드(아무 CBRAIN 자동 연결) 링크인가 — 지정 번호 없음."""
        return self.target_id == TARGET_ANY

    def id_mismatch(self) -> bool:
        """지정한 번호와 실제 수신 번호가 다른가 (엉뚱한 기기 기록 방지).
        와일드카드는 지정 번호가 없으므로 불일치 개념이 없다."""
        if self.is_wildcard:
            return False
        return self.device_id is not None and self.device_id != self.target_id


class SessionManager:
    """여러 DeviceLink 를 묶어 페어링/기록을 원자적으로 제어 + 세션 산출물 관리."""

    def __init__(self, out_root: str | None = None):
        self.out_root = out_root or os.path.join(os.path.expanduser("~"), "CBRAIN_recordings")
        self.links: list[DeviceLink] = []
        self.session_dir: str | None = None
        self.recording = False
        self.t_start = 0.0
        self.events: list[tuple[float, str]] = []

    def add(self, port: str, target_id: int, bus_consumer=None) -> DeviceLink:
        link = DeviceLink(port, target_id, bus_consumer)
        self.links.append(link)
        return link

    def remove(self, link: DeviceLink) -> None:
        link.close()
        if link in self.links:
            self.links.remove(link)

    def pair_all(self) -> None:
        for link in self.links:
            if link.state == LinkState.IDLE:
                link.pair()

    def tick(self) -> None:
        now = time.monotonic()
        for link in self.links:
            link.tick(now)

    # ── 사전점검 (기록 전 게이트) ──
    def preflight(self, expected_minutes: float = 60.0) -> "PreflightReport":
        rep = PreflightReport()
        rep.check("Devices registered", bool(self.links), "Pair a device first")
        for link in self.links:
            if link.device_id is not None:
                name = f"CBRAIN_{link.device_id}"
            elif link.is_wildcard:
                name = f"dongle {os.path.basename(link.port)}"
            else:
                name = f"CBRAIN_{link.target_id}"
            rep.check(f"{name} streaming",
                      link.state in (LinkState.STREAMING, LinkState.RECORDING),
                      f"{name} not connected (state {link.state.value})")
            rep.check(f"{name} number matches",
                      not link.id_mismatch(),
                      f"{name}: target {link.target_id} ≠ actual {link.device_id}")
            # Exclude startup counter-alignment gaps; only real gaps/bad after settling.
            rep.check(f"{name} stable (no gaps/bad)",
                      link.stats.frames_bad == 0 and link.stats.recent_gaps == 0,
                      f"{name}: recent_gaps={link.stats.recent_gaps} bad={link.stats.frames_bad}")
        # Disk
        need = self._estimate_bytes(expected_minutes)
        free = self._disk_free(self.out_root)
        rep.check("Disk space", free is None or free > need * 1.2,
                  f"need≈{need/1e9:.1f}GB, free={None if free is None else free/1e9:.1f}GB")
        return rep

    def start_recording(self, meta: dict | None = None,
                        expected_minutes: float = 60.0) -> "PreflightReport":
        rep = self.preflight(expected_minutes)
        if not rep.ok:
            return rep
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = os.path.join(self.out_root, f"session_{ts}")
        os.makedirs(self.session_dir, exist_ok=True)
        self.t_start = time.time()
        self.events = []
        for link in self.links:
            link.arm_recording(self.session_dir)
        self.recording = True
        self._write_session_json(meta or {}, closing=False)
        return rep

    def mark_event(self, label: str) -> None:
        if self.recording:
            self.events.append((time.time() - self.t_start, label))

    def stop_recording(self, meta: dict | None = None) -> dict:
        summary = {"session_dir": self.session_dir, "devices": []}
        for link in self.links:
            path = link.stop_recording()
            summary["devices"].append({
                "target_id": link.target_id, "device_id": link.device_id,
                "file": path, "samples": link.stats.written_samples,
                "gaps": link.stats.gaps, "bad": link.stats.frames_bad})
        self.recording = False
        if self.session_dir:
            self._write_session_json(meta or {}, closing=True, summary=summary)
        return summary

    def close_all(self) -> None:
        for link in list(self.links):
            link.close()

    # ── 내부 ──
    def _write_session_json(self, meta, closing, summary=None):
        import json
        from datetime import datetime
        doc = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "meta": meta,
            "events": [{"t": t, "label": l} for t, l in self.events],
            "devices": [{"target_id": lk.target_id, "device_id": lk.device_id,
                         "port": lk.port} for lk in self.links],
            "closed": closing,
        }
        if summary:
            doc["summary"] = summary
        with open(os.path.join(self.session_dir, "session.json"), "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)

    def _estimate_bytes(self, minutes: float) -> float:
        total = 0.0
        for lk in self.links:
            sr = lk.stats.sr_hz or 1024
            ch = lk.stats.channel_count or 4
            total += sr * ch * 2 * minutes * 60      # int16 samples + counters/ts ≈ 이 정도의 ~2x
        return total * 2.0

    @staticmethod
    def _disk_free(path: str):
        try:
            base = path
            while base and not os.path.isdir(base):
                base = os.path.dirname(base)
            st = os.statvfs(base or "/")
            return st.f_bavail * st.f_frsize
        except Exception:  # noqa: BLE001
            return None


@dataclass
class PreflightReport:
    items: list[tuple[bool, str, str]] = field(default_factory=list)   # (ok, name, detail)

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.items.append((bool(ok), name, "" if ok else detail))

    @property
    def ok(self) -> bool:
        return all(ok for ok, _, _ in self.items)

    @property
    def failures(self) -> list[str]:
        return [f"{name}: {detail}" for ok, name, detail in self.items if not ok]
