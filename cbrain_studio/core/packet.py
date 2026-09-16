"""
CB v2 wire codec — docs/packet_protocol.md 를 그대로 구현(계약, R6 문서 우선).

모든 정수 little-endian. 프레임 = [cb_hdr 10B][payload][CRC16 2B],
CRC16-CCITT(F) over [hdr..payload].

프레임 타입(§4.3):
  0x01 DATA(dev→host) · 0x02 SYNC(dev→host) · 0x03 STATUS(dev→host)
  0x04 REPLY(dev→host) · 0x10 COMMAND(host→dev)

DATA payload = data_hdr(44B) + events(N×3) + samples(ch×spc×2) + pad(0..3).
data_hdr_v2 (44B): 상세는 packet_protocol.md §6.1.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from .crc import crc16_ccitt_f
from .types import SampleBlock

# ── 프레임 상수 (§4) ──────────────────────────────────────
MAGIC = b"CB"
VER = 2
T_DATA = 0x01
T_SYNC = 0x02
T_STATUS = 0x03
T_REPLY = 0x04
T_COMMAND = 0x10
REPLY_MARKER = 0xB0            # REPLY payload 내부 마커 (CB v1 계승, §8.2)

HDR_SIZE = 10
DATA_HDR_SIZE = 44
CRC_SIZE = 2
ORD_LEN = 16

_FIXED_FMT = "<HIHHBBIIIBBBB"     # 28B (flags..pad), §6.1
_FIXED_SIZE = struct.calcsize(_FIXED_FMT)
assert _FIXED_SIZE == 28 and _FIXED_SIZE + ORD_LEN == DATA_HDR_SIZE

MIN_FRAME_LEN = HDR_SIZE + CRC_SIZE       # 12 (§4.4)
MAX_FRAME = 4096                          # §4.4 default [OPEN P3]

# ── CTRL opcodes (§8.1) ───────────────────────────────────
CMD_RHD_REG_WR   = 0x10
CMD_RHD_REG_BULK = 0x11
CMD_RHD_REG_RD   = 0x12
CMD_SET_CHMAP    = 0x20
CMD_SET_SR_HZ    = 0x21
CMD_SET_DSP      = 0x22
CMD_SET_SPC      = 0x23
CMD_START_STREAM = 0x24
CMD_STOP_STREAM  = 0x25
CMD_APPLY        = 0x2F
CMD_GET_SUMMARY  = 0x30
CMD_GET_CAPS     = 0x31
CMD_SET_ADV_NAME = 0x40
CMD_GET_BAT      = 0x41
CMD_GET_FW_VER   = 0x42
CMD_SET_LED      = 0x50   # [led_index][r][g][b]
CMD_SET_LED_MODE = 0x51   # [led_index][mode][params...]
CMD_SYNC_CONFIG  = 0x60   # [source_id][edge_mask][reset]

# 이벤트 코드 (§6.5)
EVT_LED0 = 1
EVT_LED1 = 2
EVT_SYNC = 3
EVT_EVENT = 4

# DATA 헤더 flags 비트 (§6.1). bit0/1 = OVFL/DROPPED. bit8/9 = 기기 실측 LED0/1 on/off,
# bit15 = 이 기기가 LED 상태를 보고함(구형 펌웨어는 flags=0 → LED 비트 신뢰 금지).
FLAG_OVFL = 0x0001
FLAG_DROPPED = 0x0002
FLAG_LED0 = 0x0100
FLAG_LED1 = 0x0200
FLAG_LED_VALID = 0x8000


def led_state(flags: int) -> list[bool] | None:
    """DATA flags 에서 기기 실측 LED 상태. LED 를 보고하지 않는 펌웨어면 None."""
    if not (flags & FLAG_LED_VALID):
        return None
    return [bool(flags & FLAG_LED0), bool(flags & FLAG_LED1)]


# ══════════════════════════════════════════════════════════
# 디코드 결과 타입
# ══════════════════════════════════════════════════════════
@dataclass
class SyncFrame:              # §7
    device_id: int
    source_id: int
    edge: int
    seq_sync: int
    dev_tick: int
    sample_idx: int


@dataclass
class StatusFrame:            # §8.3
    device_id: int
    batt_mv: int
    batt_pct: int
    link_rssi: int
    led_state: list[int]


@dataclass
class ReplyFrame:             # §8.2  [0xB0][opcode][status][data...]
    opcode: int
    status: int
    data: bytes


@dataclass
class Capabilities:          # §8.4  GET_CAPS reply_data
    proto_ver: int
    fw: tuple[int, int, int]
    hw_rev: int
    max_channels: int
    led_count: int
    sync_inputs: int
    enc: int
    uv_per_lsb: float
    rates: list[int] = field(default_factory=list)


# ══════════════════════════════════════════════════════════
# 디코더 (§4.4 재조립)
# ══════════════════════════════════════════════════════════
@dataclass
class DecodeResult:
    blocks: list[SampleBlock] = field(default_factory=list)
    syncs: list[SyncFrame] = field(default_factory=list)
    statuses: list[StatusFrame] = field(default_factory=list)
    replies: list[ReplyFrame] = field(default_factory=list)


class Decoder:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.bytes_in = 0
        self.frames_ok = 0
        self.frames_bad = 0
        self.last_seq: int | None = None
        self.seq_gaps = 0

    def decode(self, chunk: bytes) -> DecodeResult:
        res = DecodeResult()
        if chunk:
            self.bytes_in += len(chunk)
            self.buf.extend(chunk)

        i, n = 0, len(self.buf)
        while n - i >= MIN_FRAME_LEN:
            if not (self.buf[i] == 0x43 and self.buf[i + 1] == 0x42):  # 'C','B'
                j = self.buf.find(MAGIC, i + 1)
                if j < 0:
                    i = n - 1
                    break
                i = j
                continue

            ver = self.buf[i + 2]
            ftype = self.buf[i + 3]
            seq = int.from_bytes(self.buf[i + 4:i + 8], "little")
            total_len = int.from_bytes(self.buf[i + 8:i + 10], "little")
            if ver != VER or not (MIN_FRAME_LEN <= total_len <= MAX_FRAME):
                i += 1
                continue
            if i + total_len > n:
                break

            frame = bytes(self.buf[i:i + total_len])
            if crc16_ccitt_f(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
                self.frames_bad += 1
                i += 1
                continue

            if self._dispatch(ftype, frame, res):
                self.frames_ok += 1
                if self.last_seq is not None and seq != (self.last_seq + 1) & 0xFFFFFFFF:
                    self.seq_gaps += 1
                self.last_seq = seq
            else:
                self.frames_bad += 1
            i += total_len

        if i:
            del self.buf[:i]
        return res

    def _dispatch(self, ftype: int, frame: bytes, res: DecodeResult) -> bool:
        payload = frame[HDR_SIZE:-CRC_SIZE]
        if ftype == T_DATA:
            blk = _parse_data(frame)
            if blk is None:
                return False
            res.blocks.append(blk)
        elif ftype == T_SYNC:
            s = _parse_sync(payload)
            if s is None:
                return False
            res.syncs.append(s)
        elif ftype == T_STATUS:
            st = _parse_status(payload)
            if st is None:
                return False
            res.statuses.append(st)
        elif ftype == T_REPLY:
            rp = _parse_reply(payload)
            if rp is None:
                return False
            res.replies.append(rp)
        else:
            return False  # unknown type — skipped by length (§11)
        return True


def _parse_data(frame: bytes) -> SampleBlock | None:
    total_len = len(frame)
    off = HDR_SIZE
    (flags, device_id, sr_hz, ch_map, ch_count, spc,
     first_counter, t0_tick, tick_hz, enc, ev_len, ord_len, pad) = \
        struct.unpack_from(_FIXED_FMT, frame, off)
    off += _FIXED_SIZE
    ord_bytes = frame[off:off + ORD_LEN]
    off += ORD_LEN

    if enc not in (0, 1):                       # §6.4: 0 or 1
        return None
    if not (1 <= ch_count <= 16 and 0 <= ev_len <= 255 and 0 <= ord_len <= ORD_LEN):
        return None

    data_len = ch_count * spc * 2
    if off + ev_len + data_len + pad + CRC_SIZE != total_len:
        return None

    data_off = off + ev_len
    samples = np.frombuffer(frame[data_off:data_off + data_len], dtype="<i2")
    if samples.size != ch_count * spc:
        return None
    # (spc, ch) interleaved → (ch, spc) 채널-우선
    channels = samples.reshape(spc, ch_count).T.astype(np.int16, copy=True)

    order = [b for b in ord_bytes[:ord_len] if b != 0xFF]
    if ord_len == 0 or len(order) != ch_count:
        order = list(range(ch_count))

    return SampleBlock(
        device_id=device_id, first_counter=first_counter, sr_hz=sr_hz,
        channels=channels, order=order, flags=flags, enc=enc,
    )


def _parse_sync(p: bytes) -> SyncFrame | None:
    if len(p) < 16:
        return None
    device_id, source_id, edge, seq_sync, dev_tick, sample_idx = \
        struct.unpack_from("<IBBHII", p, 0)
    return SyncFrame(device_id, source_id, edge, seq_sync, dev_tick, sample_idx)


def _parse_status(p: bytes) -> StatusFrame | None:
    if len(p) < 8:
        return None
    device_id, batt_mv, batt_pct, rssi = struct.unpack_from("<IHBb", p, 0)
    led_state = list(p[8:])
    return StatusFrame(device_id, batt_mv, batt_pct, rssi, led_state)


def _parse_reply(p: bytes) -> ReplyFrame | None:
    if len(p) < 3 or p[0] != REPLY_MARKER:
        return None
    return ReplyFrame(opcode=p[1], status=p[2], data=bytes(p[3:]))


def parse_capabilities(data: bytes) -> Capabilities | None:
    """GET_CAPS REPLY 의 reply_data 파싱 (§8.4)."""
    if len(data) < 14:
        return None
    (proto_ver, fw_major, fw_minor, fw_patch, hw_rev, max_ch, led_count,
     sync_inputs, enc) = struct.unpack_from("<BBBBBBBBB", data, 0)
    (uv_per_lsb,) = struct.unpack_from("<f", data, 9)
    n_rates = data[13]
    rates = list(struct.unpack_from("<" + "H" * n_rates, data, 14)) if n_rates else []
    return Capabilities(proto_ver, (fw_major, fw_minor, fw_patch), hw_rev,
                        max_ch, led_count, sync_inputs, enc, uv_per_lsb, rates)


# ══════════════════════════════════════════════════════════
# 인코더
# ══════════════════════════════════════════════════════════
def _wrap(seq: int, ftype: int, payload: bytes) -> bytes:
    total = HDR_SIZE + len(payload) + CRC_SIZE
    body = struct.pack("<2sBBIH", MAGIC, VER, ftype, seq & 0xFFFFFFFF, total) + payload
    return body + struct.pack("<H", crc16_ccitt_f(body))


def encode_data(seq: int, device_id: int, channels: np.ndarray, first_counter: int,
                sr_hz: int, order: list[int] | None = None, t0_tick: int = 0,
                tick_hz: int = 32768, flags: int = 0, enc: int = 0,
                events: bytes = b"") -> bytes:
    """channels: (ch, spc) int16 → CB v2 DATA 프레임."""
    ch_count, spc = channels.shape
    order = order if order is not None else list(range(ch_count))
    ord_block = bytes(order[:ORD_LEN]) + b"\xff" * (ORD_LEN - len(order))
    ch_map = 0
    for c in order:
        ch_map |= (1 << c) if c < 16 else 0
    data = channels.T.astype("<i2").tobytes()   # (ch,spc)→(spc,ch) interleave

    pre = HDR_SIZE + DATA_HDR_SIZE + len(events) + len(data)
    pad = (-(pre + CRC_SIZE)) % 4
    fixed = struct.pack(_FIXED_FMT, flags, device_id, sr_hz, ch_map, ch_count, spc,
                        first_counter, t0_tick, tick_hz, enc, len(events), len(order), pad)
    payload = fixed + ord_block + events + data + b"\x00" * pad
    return _wrap(seq, T_DATA, payload)


def encode_sync(seq, device_id, source_id=0, edge=1, seq_sync=0, dev_tick=0, sample_idx=0) -> bytes:
    return _wrap(seq, T_SYNC, struct.pack("<IBBHII", device_id, source_id, edge,
                                          seq_sync, dev_tick, sample_idx))


def encode_status(seq, device_id, batt_mv, batt_pct, link_rssi, led_state=()) -> bytes:
    payload = struct.pack("<IHBb", device_id, batt_mv, batt_pct, link_rssi) + bytes(led_state)
    return _wrap(seq, T_STATUS, payload)


def encode_reply(seq, opcode, status=0x00, data=b"") -> bytes:
    return _wrap(seq, T_REPLY, bytes([REPLY_MARKER, opcode, status]) + data)


def encode_capabilities(seq, caps: Capabilities) -> bytes:
    fw = caps.fw
    body = struct.pack("<BBBBBBBBB", caps.proto_ver, fw[0], fw[1], fw[2], caps.hw_rev,
                       caps.max_channels, caps.led_count, caps.sync_inputs, caps.enc)
    body += struct.pack("<f", caps.uv_per_lsb)
    body += bytes([len(caps.rates)]) + struct.pack("<" + "H" * len(caps.rates), *caps.rates)
    return encode_reply(seq, CMD_GET_CAPS, 0x00, body)


# ── 커맨드(host→device) ────────────────────────────────────
def build_command(opcode: int, args: bytes = b"") -> bytes:
    """CRC 프레임 없는 payload [opcode][args] (P7 저지연 옵션용)."""
    return bytes([opcode]) + args


def encode_command(seq: int, opcode: int, args: bytes = b"") -> bytes:
    """CRC-framed COMMAND (type 0x10), §8.1 표준."""
    return _wrap(seq, T_COMMAND, build_command(opcode, args))


def cmd_set_chmap(channels) -> bytes:
    """SET_CHMAP (§8.1): channels=int N → 물리채널 0..N-1, 또는 명시 리스트.
    args = [ch_map u16][ord_len u8][ord…]."""
    order = list(range(channels)) if isinstance(channels, int) else list(channels)
    ch_map = 0
    for c in order:
        ch_map |= (1 << c) if 0 <= c < 16 else 0
    return build_command(CMD_SET_CHMAP, struct.pack("<H", ch_map) + bytes([len(order)]) + bytes(order))


def cmd_set_sr_hz(sr): return build_command(CMD_SET_SR_HZ, struct.pack("<H", sr))
def cmd_set_spc(log2): return build_command(CMD_SET_SPC, bytes([log2]))
def cmd_set_led(idx, r, g, b): return build_command(CMD_SET_LED, bytes([idx, r, g, b]))
def cmd_start_stream(): return build_command(CMD_START_STREAM)
def cmd_stop_stream(): return build_command(CMD_STOP_STREAM)
def cmd_apply(): return build_command(CMD_APPLY)
def cmd_get_caps(): return build_command(CMD_GET_CAPS)
def cmd_get_bat(): return build_command(CMD_GET_BAT)
def cmd_get_fw_version(): return build_command(CMD_GET_FW_VER)
