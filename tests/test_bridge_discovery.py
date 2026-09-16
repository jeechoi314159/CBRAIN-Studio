"""Wire framing tests for the USB-only discovery diagnostic (no hardware)."""
import binascii
import unittest
from tools.bridge_discover import Decoder, UP_MAGIC


def frame(kind, payload):
    body = bytes([kind]) + len(payload).to_bytes(2, 'little') + payload
    return UP_MAGIC + body + binascii.crc_hqx(body, 0xffff).to_bytes(2, 'little')


class DiscoveryDecoderTests(unittest.TestCase):
    def test_every_fragment_boundary_and_embedded_magic(self):
        payload = bytes(range(256))
        wire = frame(0x90, payload) + frame(0x92, UP_MAGIC)
        for cut in range(len(wire) + 1):
            d = Decoder()
            self.assertEqual(d.feed(wire[:cut]) + d.feed(wire[cut:]), [(0x90, payload), (0x92, UP_MAGIC)])

    def test_one_byte_chunks(self):
        d = Decoder()
        result = []
        for b in frame(0x91, bytes([1, 1, 0, 0, 32, 15])):
            result.extend(d.feed(bytes([b])))
        self.assertEqual(result, [(0x91, bytes([1, 1, 0, 0, 32, 15]))])

    def test_corruption_and_invalid_length_resync(self):
        d = Decoder()
        corrupt = bytearray(frame(0x92, b'candidate'))
        corrupt[-1] ^= 1
        invalid = UP_MAGIC + b'\x90\xff\xff'
        self.assertEqual(d.feed(b'old raw data' + corrupt + invalid + frame(0x93, b'done')), [(0x93, b'done')])
        self.assertEqual(d.bad, 2)

    def test_no_magic_bounds_buffer(self):
        d = Decoder()
        self.assertEqual(d.feed(b'x' * 100000), [])
        self.assertLessEqual(len(d.buf), 3)

if __name__ == '__main__':
    unittest.main()
