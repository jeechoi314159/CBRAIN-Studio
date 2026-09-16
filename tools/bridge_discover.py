#!/usr/bin/env python3
"""USB-only discovery diagnostic for bridge discovery firmware 1.0.0.
Close Pairer/Studio first. This command explicitly clears that dongle's target.
Usage: python tools/bridge_discover.py --port /dev/cu.usbmodem1101
No Mac Bluetooth API is imported or used. Does not connect to a headstage.
"""
from __future__ import annotations
import argparse
import binascii
import struct
import time

MAGIC = b'\xcb\x1d\x70\x2a'
UP_MAGIC = b'\xcb\x1d\x70\x2b'

class Decoder:
    def __init__(self):
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data):
        self.buf.extend(data)
        result = []
        while len(self.buf) >= 7:
            offset = self.buf.find(UP_MAGIC)
            if offset < 0:
                del self.buf[:-3]
                break
            if offset:
                del self.buf[:offset]
            if len(self.buf) < 7:
                break
            n = int.from_bytes(self.buf[5:7], 'little')
            if n > 256:
                self.bad += 1
                del self.buf[0]
                continue
            if len(self.buf) < n + 9:
                break
            frame = bytes(self.buf[:n+9])
            crc = binascii.crc_hqx(frame[4:-2], 0xffff)
            if crc != int.from_bytes(frame[-2:], 'little'):
                self.bad += 1
                del self.buf[0]
                continue
            del self.buf[:n+9]
            result.append((frame[4], frame[7:-2]))
        return result


def discover(port, seconds):
    import serial
    decoder = Decoder()
    with serial.Serial(port, 1_000_000, timeout=0.1) as ser:
        ser.reset_input_buffer()
        ser.write(MAGIC + b'\x01' + bytes(4))  # legacy idle; also restore raw mode
        # Retry capability handshake until the asynchronous disconnect completes.
        until = time.monotonic() + 5
        next_probe = 0
        ready = False
        while time.monotonic() < until and not ready:
            if time.monotonic() >= next_probe:
                ser.write(MAGIC + b'\x10')
                next_probe = time.monotonic() + 0.5
            for kind, payload in decoder.feed(ser.read(ser.in_waiting or 1)):
                if kind == 0x91 and len(payload) == 6 and payload[0] == 1:
                    print(f'Bridge firmware {payload[1]}.{payload[2]}.{payload[3]}, capacity {payload[4]}')
                    ready = True
        if not ready:
            raise RuntimeError('Discovery capability unavailable: update dongle firmware or check port ownership.')
        request = 1
        ser.write(MAGIC + b'\x11' + struct.pack('<HI', int(seconds*1000), request))
        until = time.monotonic() + seconds + 5
        while time.monotonic() < until:
            for kind, p in decoder.feed(ser.read(ser.in_waiting or 1)):
                if kind == 0x80 and len(p) == 6 and p[1]:
                    raise RuntimeError(f'Bridge command 0x{p[0]:02x} failed, status={p[1]}, detail={int.from_bytes(p[2:], "little")}')
                if kind == 0x92 and len(p) == 24:
                    req, token, device = struct.unpack('<III', p[:12])
                    if req != request:
                        continue
                    address = ':'.join(f'{b:02X}' for b in p[13:19][::-1])
                    rssi = struct.unpack('b', p[19:20])[0]
                    print(f'CBRAIN_{device}  RSSI={rssi} dBm  address={address} type={p[12]} token={token}')
                if kind == 0x93 and len(p) == 11 and int.from_bytes(p[:4], 'little') == request:
                    print(f'Discovery done: {p[5]} candidates, overflow={bool(p[6])}, TX dropped={int.from_bytes(p[7:], "little")}, bad envelopes={decoder.bad}')
                    # Return the idle dongle to the old Pairer-compatible raw mode.
                    ser.write(MAGIC + b'\x01' + bytes(4))
                    ser.flush()
                    return
        raise RuntimeError('Discovery timed out; reconnect dongle and retry.')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', required=True)
    ap.add_argument('--seconds', type=float, default=8)
    args = ap.parse_args()
    if not 0.1 <= args.seconds <= 30:
        ap.error('--seconds must be 0.1–30')
    try:
        discover(args.port, args.seconds)
    except (RuntimeError, OSError, ImportError) as exc:
        ap.exit(1, f'{exc}\n')

if __name__ == '__main__':
    main()
