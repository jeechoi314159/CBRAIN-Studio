"""
HexStamper — 설정 blob 을 base.hex 의 예약 주소에 병합해 <name>.hex 생성 (외부 의존 X).

범용 base.hex(펌웨어)는 부팅 시 CB_CONFIG_ADDR 에서 설정을 읽는다. 이 모듈은 base.hex 를
Intel HEX 로 파싱해 그 주소에 설정 바이트를 덮어쓰고 다시 emit 한다. 툴체인/라이브러리
불필요 → GUI 가 어디서든 <name>.hex 를 찍을 수 있다.
"""
from __future__ import annotations


def _checksum(rec: bytes) -> int:
    return (-sum(rec)) & 0xFF


def parse_hex(text: str) -> tuple[dict[int, int], int | None]:
    """Intel HEX → {절대주소: 바이트}, start_linear_address(있으면)."""
    mem: dict[int, int] = {}
    upper = 0
    start_addr: int | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != ":":
            continue
        raw = bytes.fromhex(line[1:])
        ln, addr_hi, addr_lo, rtype = raw[0], raw[1], raw[2], raw[3]
        data = raw[4:4 + ln]
        if _checksum(raw[:-1]) != raw[-1]:
            raise ValueError(f"hex checksum error: {line}")
        addr = (addr_hi << 8) | addr_lo
        if rtype == 0x00:                       # data
            for i, b in enumerate(data):
                mem[upper + addr + i] = b
        elif rtype == 0x02:                     # extended segment address (addr = seg<<4 + off)
            upper = ((data[0] << 8) | data[1]) << 4
        elif rtype == 0x04:                     # extended linear address
            upper = ((data[0] << 8) | data[1]) << 16
        elif rtype == 0x05:                     # start linear address
            start_addr = int.from_bytes(data, "big")
        elif rtype == 0x01:                     # EOF
            break
        # 0x03 (start segment address) → 무시 (플래시에 불필요)
    return mem, start_addr


def emit_hex(mem: dict[int, int], start_addr: int | None = None) -> str:
    """{주소: 바이트} → Intel HEX 텍스트 (16B/레코드, 64KB 경계에서 ELA)."""
    out: list[str] = []

    def rec(ln, addr, rtype, data: bytes):
        body = bytes([ln, (addr >> 8) & 0xFF, addr & 0xFF, rtype]) + data
        out.append(":" + (body + bytes([_checksum(body)])).hex().upper())

    cur_upper = None
    addrs = sorted(mem)
    i = 0
    while i < len(addrs):
        a = addrs[i]
        upper = a & 0xFFFF0000
        if upper != cur_upper:
            rec(2, 0, 0x04, bytes([(upper >> 24) & 0xFF, (upper >> 16) & 0xFF]))
            cur_upper = upper
        # 같은 64KB 안에서 최대 16B 연속 런
        chunk = [mem[a]]
        j = i + 1
        while (j < len(addrs) and addrs[j] == addrs[j - 1] + 1
               and len(chunk) < 16 and (addrs[j] & 0xFFFF0000) == upper):
            chunk.append(mem[addrs[j]])
            j += 1
        rec(len(chunk), a & 0xFFFF, 0x00, bytes(chunk))
        i = j
    if start_addr is not None:
        rec(4, 0, 0x05, start_addr.to_bytes(4, "big"))
    rec(0, 0, 0x01, b"")
    return "\n".join(out) + "\n"


def stamp_config(base_hex_text: str, config_blob: bytes, addr: int) -> str:
    """base.hex 텍스트에 config_blob 을 addr 에 덮어써서 새 HEX 텍스트 반환."""
    mem, start = parse_hex(base_hex_text)
    for i, b in enumerate(config_blob):
        mem[addr + i] = b
    return emit_hex(mem, start)


def read_region(hex_text: str, addr: int, length: int) -> bytes:
    """HEX 에서 addr..addr+length 바이트를 읽음 (검증용). 빈 곳은 0xFF."""
    mem, _ = parse_hex(hex_text)
    return bytes(mem.get(addr + i, 0xFF) for i in range(length))
