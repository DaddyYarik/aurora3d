"""Dependency-free tests for Aurora3D.

Covers the from-scratch GIF/LZW encoder (round-trip + full decode of a real
exported GIF) and the shape generators. Runs on pure stdlib so CI needs nothing
beyond Python itself.
"""

import os
import random
import struct
import tempfile
import unittest

import aurora


# --------------------------------------------------------------------------- #
#  Independent reference decoders (not used by aurora itself)
# --------------------------------------------------------------------------- #

def lzw_decode(data, mcs=8):
    clear = 1 << mcs
    end = clear + 1
    code_size = mcs + 1
    bits = nbits = pos = 0

    def read():
        nonlocal bits, nbits, pos
        while nbits < code_size:
            bits |= data[pos] << nbits
            pos += 1
            nbits += 8
        v = bits & ((1 << code_size) - 1)
        bits >>= code_size
        nbits -= code_size
        return v

    out = bytearray()
    table = []

    def reset():
        nonlocal table, code_size
        table = [bytes([i]) for i in range(clear)] + [None, None]
        code_size = mcs + 1

    reset()
    prev = None
    while True:
        code = read()
        if code == clear:
            reset()
            prev = None
            continue
        if code == end:
            break
        if prev is None:
            out += table[code]
            prev = code
            continue
        if code < len(table) and table[code] is not None:
            entry = table[code]
        elif code == len(table):
            entry = table[prev] + table[prev][:1]
        else:
            raise ValueError(f"bad code {code} (table={len(table)})")
        out += entry
        table.append(table[prev] + entry[:1])
        if len(table) == (1 << code_size) and code_size < 12:
            code_size += 1
        prev = code
    return bytes(out)


def parse_gif(raw):
    """Parse a GIF and return (width, height, [(fw, fh, pixel_indices), ...])."""
    assert raw[:6] in (b"GIF87a", b"GIF89a"), "missing GIF signature"
    assert raw[-1] == 0x3B, "missing GIF trailer"
    w, h = struct.unpack_from("<HH", raw, 6)
    packed = raw[10]
    pos = 13
    if packed & 0x80:
        pos += (2 ** ((packed & 0x07) + 1)) * 3  # skip global color table
    frames = []
    while pos < len(raw):
        block = raw[pos]
        if block == 0x3B:
            break
        if block == 0x21:                         # extension: skip sub-blocks
            pos += 2
            while raw[pos] != 0:
                pos += raw[pos] + 1
            pos += 1
        elif block == 0x2C:                       # image descriptor
            pos += 1
            _, _, fw, fh = struct.unpack_from("<HHHH", raw, pos)
            pos += 8
            ipacked = raw[pos]
            pos += 1
            if ipacked & 0x80:
                pos += (2 ** ((ipacked & 0x07) + 1)) * 3  # local color table
            mcs = raw[pos]
            pos += 1
            data = bytearray()
            while raw[pos] != 0:
                n = raw[pos]
                pos += 1
                data += raw[pos:pos + n]
                pos += n
            pos += 1
            frames.append((fw, fh, lzw_decode(bytes(data), mcs)))
        else:
            raise ValueError(f"unexpected block 0x{block:02x} at offset {pos}")
    return w, h, frames


# --------------------------------------------------------------------------- #
#  Tests
# --------------------------------------------------------------------------- #

class TestLZW(unittest.TestCase):
    def test_roundtrip_random(self):
        rng = random.Random(42)
        for _ in range(120):
            n = rng.randint(0, 6000)
            src = bytes(rng.randrange(256) for _ in range(n))
            self.assertEqual(lzw_decode(aurora.lzw_encode(src, 8), 8), src)

    def test_roundtrip_forces_table_reset(self):
        # 20k bytes drives the dictionary to 12 bits and the clear/reset path.
        src = bytes((i // 5) % 256 for i in range(20000))
        self.assertEqual(lzw_decode(aurora.lzw_encode(src, 8), 8), src)

    def test_empty(self):
        self.assertEqual(lzw_decode(aurora.lzw_encode(b"", 8), 8), b"")


class TestShapes(unittest.TestCase):
    def test_generators_have_points_and_normals(self):
        for fn in (aurora.make_torus, aurora.make_sphere,
                   aurora.make_cube, aurora.make_ripple):
            pts = fn()
            self.assertGreater(len(pts), 100)
            point, normal = pts[0]
            self.assertEqual(len(point), 3)
            self.assertEqual(len(normal), 3)

    def test_text_builds_geometry(self):
        self.assertGreater(len(aurora.make_text("HI")), 100)


class TestGifExport(unittest.TestCase):
    def _export(self, *extra):
        fd, path = tempfile.mkstemp(suffix=".gif")
        os.close(fd)
        try:
            argv = ["--gif", path, "--gif-size", "64x48",
                    "--gif-frames", "8", "--gif-fps", "20", *extra]
            self.assertEqual(aurora.main(argv), 0)
            with open(path, "rb") as fh:
                return fh.read()
        finally:
            os.remove(path)

    def test_single_shape_gif_fully_decodes(self):
        w, h, frames = parse_gif(self._export())
        self.assertEqual((w, h), (64, 48))
        self.assertEqual(len(frames), 8)
        for fw, fh, pixels in frames:
            self.assertEqual(len(pixels), fw * fh)

    def test_dual_shape_gif_fully_decodes(self):
        w, h, frames = parse_gif(self._export("--dual"))
        self.assertEqual(len(frames), 8)
        for fw, fh, pixels in frames:
            self.assertEqual(len(pixels), fw * fh)


if __name__ == "__main__":
    unittest.main()
