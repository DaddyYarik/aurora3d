#!/usr/bin/env python3
"""
Aurora3D — a real-time 3D engine that runs entirely in your terminal.

No dependencies. Just the Python standard library.

Features
--------
* Solid-shaded 3D rendering with per-pixel surface normals + a z-buffer
* TWO orbiting colored light sources (warm + cool) with diffuse & specular
* 24-bit truecolor output; complementary light hues that can cycle
* A parallax twinkling starfield behind the scene
* Built-in shapes: torus, sphere, cube, ripple plane
* Load any Wavefront .OBJ model and render it as a solid point cloud
* Turn text into chunky extruded 3D letters
* Interactive orbit camera (WASD) on top of the auto-spin
* Cross-platform: enables ANSI + UTF-8 on Windows, raw keyboard on POSIX

Controls (interactive terminal only)
-------------------------------------
  space / n   next shape           w a s d   orbit the camera
  c           cycle light colors   p         pause / resume auto-spin
  l           freeze / orbit light + / -     spin faster / slower
  s           toggle starfield     r         recenter camera
  q           quit  (Ctrl-C also works)

Try it
------
  python aurora.py                       # interactive demo
  python aurora.py --shape sphere        # start on a specific built-in shape
  python aurora.py --text "AURORA 3D"    # render extruded 3D text
  python aurora.py --obj model.obj       # load & render an .obj model
  python aurora.py --frames 120          # render 120 frames then exit
  python aurora.py --no-color            # luminance-only ASCII fallback
"""

from __future__ import annotations

import argparse
import math
import os
import random
import shutil
import struct
import sys
import time

ESC = "\x1b"


# --------------------------------------------------------------------------- #
#  Terminal plumbing
# --------------------------------------------------------------------------- #

def enable_ansi() -> None:
    """Switch the console into virtual-terminal mode on Windows and force
    UTF-8 stdout so escape codes and unicode glyphs survive."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def term_size(default=(100, 38)) -> tuple[int, int]:
    cols, rows = shutil.get_terminal_size(default)
    return cols, max(8, rows - 1)  # one row reserved for the HUD


def hide_cursor() -> None:
    sys.stdout.write(f"{ESC}[?25l")


def show_cursor() -> None:
    sys.stdout.write(f"{ESC}[?25h")


def clear() -> None:
    sys.stdout.write(f"{ESC}[2J{ESC}[H")


def home() -> None:
    sys.stdout.write(f"{ESC}[H")


# --------------------------------------------------------------------------- #
#  Non-blocking, cross-platform keyboard
# --------------------------------------------------------------------------- #

class Keyboard:
    def __init__(self) -> None:
        self.enabled = sys.stdin.isatty()
        self._posix_state = None

    def __enter__(self) -> "Keyboard":
        if self.enabled and os.name != "nt":
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._posix_state = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._posix_state is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._posix_state)

    def get(self) -> str | None:
        if not self.enabled:
            return None
        if os.name == "nt":
            import msvcrt

            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\x00", b"\xe0"):   # function / arrow prefix
                    msvcrt.getch()             # swallow the scan code
                    return None
                try:
                    return ch.decode("utf-8", "ignore")
                except Exception:
                    return None
            return None
        else:
            import select

            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                return sys.stdin.read(1)
            return None


# --------------------------------------------------------------------------- #
#  Tiny 3D math
# --------------------------------------------------------------------------- #

def normalize(v):
    x, y, z = v
    m = math.sqrt(x * x + y * y + z * z) or 1.0
    return (x / m, y / m, z / m)


def cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def rotation_matrix(ax, ay, az):
    """R = Rz * Ry * Rx, returned as a flat 9-tuple."""
    sa, ca = math.sin(ax), math.cos(ax)
    sb, cb = math.sin(ay), math.cos(ay)
    sc, cc = math.sin(az), math.cos(az)
    return (
        cc * cb, cc * sb * sa - sc * ca, cc * sb * ca + sc * sa,
        sc * cb, sc * sb * sa + cc * ca, sc * sb * ca - cc * sa,
        -sb,     cb * sa,                cb * ca,
    )


def apply(m, v):
    x, y, z = v
    return (
        m[0] * x + m[1] * y + m[2] * z,
        m[3] * x + m[4] * y + m[5] * z,
        m[6] * x + m[7] * y + m[8] * z,
    )


def matmul(a, b):
    """Multiply two flat 3x3 matrices, returning a flat 9-tuple."""
    return (
        a[0]*b[0]+a[1]*b[3]+a[2]*b[6], a[0]*b[1]+a[1]*b[4]+a[2]*b[7], a[0]*b[2]+a[1]*b[5]+a[2]*b[8],
        a[3]*b[0]+a[4]*b[3]+a[5]*b[6], a[3]*b[1]+a[4]*b[4]+a[5]*b[7], a[3]*b[2]+a[4]*b[5]+a[5]*b[8],
        a[6]*b[0]+a[7]*b[3]+a[8]*b[6], a[6]*b[1]+a[7]*b[4]+a[8]*b[7], a[6]*b[2]+a[7]*b[5]+a[8]*b[8],
    )


def normalize_cloud(points, radius=2.6):
    """Center a (point, normal) cloud at the origin and scale to fit `radius`."""
    if not points:
        return points
    xs = [p[0][0] for p in points]
    ys = [p[0][1] for p in points]
    zs = [p[0][2] for p in points]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    cz = (min(zs) + max(zs)) / 2
    far = max(math.sqrt((p[0][0] - cx) ** 2 + (p[0][1] - cy) ** 2 + (p[0][2] - cz) ** 2)
              for p in points) or 1.0
    s = radius / far
    return [(((p[0][0] - cx) * s, (p[0][1] - cy) * s, (p[0][2] - cz) * s), p[1])
            for p in points]


# --------------------------------------------------------------------------- #
#  Built-in shapes — each returns a list of (point, normal)
# --------------------------------------------------------------------------- #

def make_torus(r_tube=1.0, r_ring=2.2, n_theta=90, n_phi=200):
    pts = []
    for i in range(n_theta):
        th = 2 * math.pi * i / n_theta
        ct, st = math.cos(th), math.sin(th)
        for j in range(n_phi):
            ph = 2 * math.pi * j / n_phi
            cp, sp = math.cos(ph), math.sin(ph)
            base = r_ring + r_tube * ct
            pts.append(((base * cp, base * sp, r_tube * st), (ct * cp, ct * sp, st)))
    return pts


def make_sphere(r=2.6, n_theta=110, n_phi=200):
    pts = []
    for i in range(1, n_theta):
        th = math.pi * i / n_theta
        st, ct = math.sin(th), math.cos(th)
        for j in range(n_phi):
            ph = 2 * math.pi * j / n_phi
            n = (st * math.cos(ph), st * math.sin(ph), ct)
            pts.append(((r * n[0], r * n[1], r * n[2]), n))
    return pts


def make_box(center, half, n=5):
    """Sample the 6 faces of an axis-aligned box. `half` = (hx, hy, hz)."""
    cx, cy, cz = center
    hx, hy, hz = half
    pts = []
    faces = [
        ((1, 0, 0), (0, hy, 0), (0, 0, hz), hx),
        ((-1, 0, 0), (0, hy, 0), (0, 0, hz), hx),
        ((0, 1, 0), (hx, 0, 0), (0, 0, hz), hy),
        ((0, -1, 0), (hx, 0, 0), (0, 0, hz), hy),
        ((0, 0, 1), (hx, 0, 0), (0, hy, 0), hz),
        ((0, 0, -1), (hx, 0, 0), (0, hy, 0), hz),
    ]
    for normal, u, v, d in faces:
        for i in range(n):
            a = (i / (n - 1) * 2 - 1) if n > 1 else 0.0
            for j in range(n):
                b = (j / (n - 1) * 2 - 1) if n > 1 else 0.0
                pts.append((
                    (cx + normal[0] * d + u[0] * a + v[0] * b,
                     cy + normal[1] * d + u[1] * a + v[1] * b,
                     cz + normal[2] * d + u[2] * a + v[2] * b),
                    normal,
                ))
    return pts


def make_cube(half=2.0, n=46):
    return make_box((0, 0, 0), (half, half, half), n)


def make_ripple(size=3.2, n=130, freq=2.4, amp=0.55):
    pts = []
    for i in range(n):
        x = (i / (n - 1) * 2 - 1) * size
        for j in range(n):
            y = (j / (n - 1) * 2 - 1) * size
            r = math.sqrt(x * x + y * y)
            z = amp * math.sin(freq * r)
            dz = amp * freq * math.cos(freq * r)
            nx, ny = (-dz * x / r, -dz * y / r) if r > 1e-6 else (0.0, 0.0)
            pts.append(((x, y, z), normalize((nx, ny, 1.0))))
    return pts


# --------------------------------------------------------------------------- #
#  Wavefront .OBJ loader — sampled into a solid point cloud
# --------------------------------------------------------------------------- #

def load_obj(path, target_points=26000):
    verts = []
    tris = []  # (i0, i1, i2) zero-based
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("v "):
                _, x, y, z = line.split()[:4]
                verts.append((float(x), float(y), float(z)))
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    v = tok.split("/")[0]
                    if v:
                        n = int(v)
                        idx.append(n - 1 if n > 0 else len(verts) + n)
                for k in range(1, len(idx) - 1):  # fan triangulation
                    tris.append((idx[0], idx[k], idx[k + 1]))

    if not verts or not tris:
        raise ValueError(f"'{path}' has no usable geometry (v/f lines).")

    # Per-triangle areas to drive sampling density.
    geo = []
    total_area = 0.0
    for i0, i1, i2 in tris:
        a, b, c = verts[i0], verts[i1], verts[i2]
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        cr = cross(ab, ac)
        area = 0.5 * math.sqrt(cr[0] ** 2 + cr[1] ** 2 + cr[2] ** 2)
        if area <= 0:
            continue
        n = normalize(cr)
        geo.append((a, ab, ac, n, area))
        total_area += area

    rng = random.Random(1234)
    density = target_points / total_area if total_area else 0.0
    pts = []
    for a, ab, ac, n, area in geo:
        count = max(1, min(4000, int(area * density)))
        for _ in range(count):
            r1, r2 = rng.random(), rng.random()
            if r1 + r2 > 1.0:
                r1, r2 = 1.0 - r1, 1.0 - r2
            pts.append((
                (a[0] + ab[0] * r1 + ac[0] * r2,
                 a[1] + ab[1] * r1 + ac[1] * r2,
                 a[2] + ab[2] * r1 + ac[2] * r2),
                n,
            ))
    return normalize_cloud(pts, radius=2.7)


# --------------------------------------------------------------------------- #
#  3D text — a compact 5x7 bitmap font extruded into chunky blocks
# --------------------------------------------------------------------------- #

FONT = {
    'A': ["01110","10001","10001","11111","10001","10001","10001"],
    'B': ["11110","10001","10001","11110","10001","10001","11110"],
    'C': ["01110","10001","10000","10000","10000","10001","01110"],
    'D': ["11100","10010","10001","10001","10001","10010","11100"],
    'E': ["11111","10000","10000","11110","10000","10000","11111"],
    'F': ["11111","10000","10000","11110","10000","10000","10000"],
    'G': ["01110","10001","10000","10111","10001","10001","01111"],
    'H': ["10001","10001","10001","11111","10001","10001","10001"],
    'I': ["01110","00100","00100","00100","00100","00100","01110"],
    'J': ["00111","00010","00010","00010","00010","10010","01100"],
    'K': ["10001","10010","10100","11000","10100","10010","10001"],
    'L': ["10000","10000","10000","10000","10000","10000","11111"],
    'M': ["10001","11011","10101","10101","10001","10001","10001"],
    'N': ["10001","11001","11001","10101","10011","10011","10001"],
    'O': ["01110","10001","10001","10001","10001","10001","01110"],
    'P': ["11110","10001","10001","11110","10000","10000","10000"],
    'Q': ["01110","10001","10001","10001","10101","10010","01101"],
    'R': ["11110","10001","10001","11110","10100","10010","10001"],
    'S': ["01111","10000","10000","01110","00001","00001","11110"],
    'T': ["11111","00100","00100","00100","00100","00100","00100"],
    'U': ["10001","10001","10001","10001","10001","10001","01110"],
    'V': ["10001","10001","10001","10001","10001","01010","00100"],
    'W': ["10001","10001","10001","10101","10101","11011","10001"],
    'X': ["10001","10001","01010","00100","01010","10001","10001"],
    'Y': ["10001","10001","01010","00100","00100","00100","00100"],
    'Z': ["11111","00001","00010","00100","01000","10000","11111"],
    '0': ["01110","10001","10011","10101","11001","10001","01110"],
    '1': ["00100","01100","00100","00100","00100","00100","01110"],
    '2': ["01110","10001","00001","00110","01000","10000","11111"],
    '3': ["11111","00010","00100","00010","00001","10001","01110"],
    '4': ["00010","00110","01010","10010","11111","00010","00010"],
    '5': ["11111","10000","11110","00001","00001","10001","01110"],
    '6': ["00110","01000","10000","11110","10001","10001","01110"],
    '7': ["11111","00001","00010","00100","01000","01000","01000"],
    '8': ["01110","10001","10001","01110","10001","10001","01110"],
    '9': ["01110","10001","10001","01111","00001","00010","01100"],
    ' ': ["00000","00000","00000","00000","00000","00000","00000"],
    '!': ["00100","00100","00100","00100","00100","00000","00100"],
    '-': ["00000","00000","00000","11111","00000","00000","00000"],
    '.': ["00000","00000","00000","00000","00000","00000","00100"],
    '?': ["01110","10001","00001","00110","00100","00000","00100"],
}


def make_text(text, thickness=0.45, n=4):
    pts = []
    pen_x = 0
    for ch in text.upper():
        glyph = FONT.get(ch, FONT[' '])
        for row in range(7):
            for col in range(5):
                if glyph[row][col] == '1':
                    pts.extend(make_box(
                        (pen_x + col, 6 - row, 0.0),
                        (0.5, 0.5, thickness),
                        n=n,
                    ))
        pen_x += 6
    if not pts:
        pts = make_box((0, 0, 0), (0.5, 0.5, thickness), n)
    return normalize_cloud(pts, radius=3.0)


# --------------------------------------------------------------------------- #
#  Color helpers
# --------------------------------------------------------------------------- #

def hsv_to_rgb(h, s, v):
    h %= 1.0
    i = int(h * 6)
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    return [(v, t, p), (q, v, p), (p, v, t),
            (p, q, v), (t, p, v), (v, p, q)][i % 6]


GRADIENT = " .,-~:;=!*#$@"


# --------------------------------------------------------------------------- #
#  Shared rasterizer — fills one z-buffer from any number of objects, so two
#  shapes in the same scene occlude each other correctly.
#
#  draw_calls: list of (points, rot9, offset3). Each point is transformed by
#  rot9 then translated by the world-space offset3; normals are rotated only.
# --------------------------------------------------------------------------- #

def rasterize(draw_calls, w, h, scale, cx, cy, cam, aspect_y, ambient, lights):
    size = w * h
    zbuf = [0.0] * size
    br = [0.0] * size                 # brightness 0..1 for the char ramp
    cr = [0] * size                   # color channels 0..255
    cg = [0] * size
    cb = [0] * size

    prepared = []
    for direction, lcol, intensity in lights:
        lx, ly, lz = normalize(direction)
        hx, hy, hz = normalize((lx, ly, lz + 1.0))  # view dir ~ +z
        prepared.append((lx, ly, lz, hx, hy, hz, lcol, intensity))

    for points, rot, offset in draw_calls:
        ox, oy, oz = offset
        for point, normal in points:
            px, py, pz = apply(rot, point)
            px += ox
            py += oy
            pz += oz
            z = pz + cam
            if z <= 0.1:
                continue
            inv = 1.0 / z
            sx = int(cx + scale * px * inv)
            sy = int(cy - scale * py * inv * aspect_y)
            if not (0 <= sx < w and 0 <= sy < h):
                continue
            idx = sy * w + sx
            if inv <= zbuf[idx]:
                continue

            nx, ny, nz = apply(rot, normal)
            ar, ag, ab = hsv_to_rgb((0.58 + pz * 0.05) % 1.0, 0.16, 1.0)
            rr = ambient * ar
            gg = ambient * ag
            bb = ambient * ab
            for lx, ly, lz, hx, hy, hz, lcol, inten in prepared:
                d = nx * lx + ny * ly + nz * lz
                if d > 0:
                    k = d * inten
                    rr += k * lcol[0] * ar
                    gg += k * lcol[1] * ag
                    bb += k * lcol[2] * ab
                s = nx * hx + ny * hy + nz * hz
                if s > 0:
                    sp = (s ** 32) * inten
                    rr += sp * lcol[0]
                    gg += sp * lcol[1]
                    bb += sp * lcol[2]

            lum = 0.30 * rr + 0.59 * gg + 0.11 * bb
            zbuf[idx] = inv
            br[idx] = 1.0 if lum > 1.0 else lum
            cr[idx] = 255 if rr >= 1 else int(rr * 255)
            cg[idx] = 255 if gg >= 1 else int(gg * 255)
            cb[idx] = 255 if bb >= 1 else int(bb * 255)

    return zbuf, br, cr, cg, cb


# --------------------------------------------------------------------------- #
#  Renderer
# --------------------------------------------------------------------------- #

class Renderer:
    def __init__(self, width, height, color=True, stars=True):
        self.w = width
        self.h = height
        self.color = color
        self.stars = stars
        self.scale = min(width, height * 2.0) * 0.40
        self.cx = width / 2.0
        self.cy = height / 2.0
        self.cam = 7.0
        self.aspect_y = 0.5     # terminal cells are ~2x taller than wide
        self.ambient = 0.14
        self._make_stars()

    def _make_stars(self):
        self.starfield = []
        if not self.stars:
            return
        count = max(30, (self.w * self.h) // 45)
        for _ in range(count):
            self.starfield.append((
                random.randint(0, self.w - 1),
                random.randint(0, self.h - 1),
                random.random(),
                random.uniform(1.5, 4.0),
            ))

    def render(self, draw_calls, lights, t):
        zbuf, br, cr, cg, cb = rasterize(
            draw_calls, self.w, self.h, self.scale, self.cx, self.cy,
            self.cam, self.aspect_y, self.ambient, lights)
        return self._compose(zbuf, br, cr, cg, cb, t)

    def _compose(self, zbuf, br, cr, cg, cb, t):
        w, h = self.w, self.h
        out = []
        append = out.append
        last = None
        reset = f"{ESC}[0m"

        star_cells = {}
        if self.stars:
            for sx, sy, b, spd in self.starfield:
                tw = 0.5 + 0.5 * math.sin(t * spd + sx * 0.3 + sy)
                bright = b * tw
                if bright < 0.18:
                    continue
                ch = "." if bright < 0.5 else ("+" if bright < 0.8 else "*")
                g = int(120 + 135 * bright)
                star_cells[sy * w + sx] = (ch, (g, g, min(255, g + 30)))

        glen = len(GRADIENT) - 1
        for y in range(h):
            base = y * w
            for x in range(w):
                idx = base + x
                if zbuf[idx] > 0.0:
                    ch = GRADIENT[int(br[idx] * glen)]
                    color = (cr[idx], cg[idx], cb[idx]) if self.color else None
                elif idx in star_cells:
                    ch, scolor = star_cells[idx]
                    color = scolor if self.color else None
                else:
                    ch, color = " ", None

                if self.color:
                    if color != last:
                        append(reset if color is None
                               else f"{ESC}[38;2;{color[0]};{color[1]};{color[2]}m")
                        last = color
                append(ch)
            if y < h - 1:
                append("\n")
        if self.color:
            append(reset)
        return "".join(out)


# --------------------------------------------------------------------------- #
#  Scene assembly — shared by the live renderer and the GIF exporter
# --------------------------------------------------------------------------- #

def build_scene(cache, order, shape_idx, dual, sax, say, saz,
                cam_pitch, cam_yaw, scene_yaw_extra,
                hue_base, light_angle, offset):
    spin_rot = rotation_matrix(sax, say, saz)
    scene_rot = rotation_matrix(cam_pitch, cam_yaw + scene_yaw_extra, 0.0)

    warm = hsv_to_rgb(hue_base, 0.72, 1.0)
    cool = hsv_to_rgb(hue_base + 0.5, 0.72, 1.0)
    la = light_angle
    lights = [
        ((math.cos(la) * 1.4, 0.55, math.sin(la) * 1.4), warm, 0.95),
        ((math.cos(la + 2.4) * 1.4, -0.45, math.sin(la + 2.4) * 1.4), cool, 0.85),
    ]

    name_a = order[shape_idx]
    if dual:
        name_b = order[(shape_idx + 1) % len(order)]
        spin_b = rotation_matrix(-sax, say * 0.85, -saz)
        ca = matmul(scene_rot, spin_rot)
        cb = matmul(scene_rot, spin_b)
        off_a = apply(scene_rot, (offset, 0.0, 0.0))
        off_b = apply(scene_rot, (-offset, 0.0, 0.0))
        draw_calls = [(cache[name_a], ca, off_a), (cache[name_b], cb, off_b)]
    else:
        combined = matmul(scene_rot, spin_rot)
        draw_calls = [(cache[name_a], combined, (0.0, 0.0, 0.0))]
    return draw_calls, lights


# --------------------------------------------------------------------------- #
#  Animated GIF export — a from-scratch GIF89a + LZW encoder (stdlib only)
# --------------------------------------------------------------------------- #

def lzw_encode(data, mcs=8):
    clear = 1 << mcs            # clear code
    end = clear + 1            # end-of-information code
    code_size = mcs + 1
    out = bytearray()
    cur = 0
    nbits = 0

    def emit(code, size):
        nonlocal cur, nbits
        cur |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(cur & 0xFF)
            cur >>= 8
            nbits -= 8

    table = {bytes([i]): i for i in range(clear)}
    next_code = end + 1
    emit(clear, code_size)
    if not data:
        emit(end, code_size)
        if nbits:
            out.append(cur & 0xFF)
        return bytes(out)

    buf = bytes([data[0]])
    for k in data[1:]:
        wk = buf + bytes([k])
        if wk in table:
            buf = wk
        else:
            emit(table[buf], code_size)
            if next_code == 4096:
                # Dictionary full: tell the decoder to reset, then start over.
                emit(clear, code_size)
                table = {bytes([i]): i for i in range(clear)}
                next_code = end + 1
                code_size = mcs + 1
            else:
                table[wk] = next_code
                # Grow the code width BEFORE incrementing: the decoder builds
                # its table one entry behind us, so this keeps us in lockstep.
                if next_code == (1 << code_size) and code_size < 12:
                    code_size += 1
                next_code += 1
            buf = bytes([k])
    emit(table[buf], code_size)
    emit(end, code_size)
    if nbits:
        out.append(cur & 0xFF)
    return bytes(out)


class GifWriter:
    def __init__(self, path, w, h, palette):
        self.w, self.h = w, h
        self.f = open(path, "wb")
        f = self.f
        f.write(b"GIF89a")
        f.write(struct.pack("<HH", w, h))
        f.write(bytes([0xF7, 0, 0]))   # global table of 256, bg=0, aspect=0
        f.write(palette)               # 768 bytes
        f.write(b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00")  # loop forever

    def add_frame(self, indices, delay):
        f = self.f
        f.write(b"\x21\xF9\x04\x00" + struct.pack("<H", delay) + b"\x00\x00")
        f.write(b"\x2C" + struct.pack("<HHHH", 0, 0, self.w, self.h) + b"\x00")
        f.write(bytes([8]))            # LZW minimum code size
        data = lzw_encode(indices, 8)
        for i in range(0, len(data), 255):
            chunk = data[i:i + 255]
            f.write(bytes([len(chunk)]))
            f.write(chunk)
        f.write(b"\x00")               # block terminator

    def finish(self):
        self.f.write(b"\x3B")
        self.f.close()


def export_gif(path, order, cache, start_idx, dual, args):
    try:
        W, H = (int(x) for x in args.gif_size.lower().split("x"))
    except Exception:
        print(f"aurora3d: bad --gif-size '{args.gif_size}', expected WxH",
              file=sys.stderr)
        return 1
    frames = max(1, args.gif_frames)
    delay = max(2, round(100.0 / max(1.0, args.gif_fps)))
    scale = min(W, H) * 0.42
    cx, cy = W / 2.0, H / 2.0
    cam = 9.5 if dual else 7.0
    offset = 3.2
    ambient = 0.14

    # Build a 6x6x6 (216-color) cube palette and a fast channel lookup.
    lut = [(c * 5 + 127) // 255 for c in range(256)]
    palette = bytearray(768)
    for lr in range(6):
        for lg in range(6):
            for lb in range(6):
                i = (lr * 36 + lg * 6 + lb) * 3
                palette[i], palette[i + 1], palette[i + 2] = lr * 51, lg * 51, lb * 51

    # Static starfield baked into the background (kept seamless for looping).
    star = {}
    if not args.no_stars:
        rng = random.Random(7)
        for _ in range(max(40, (W * H) // 900)):
            sx, sy, b = rng.randrange(W), rng.randrange(H), rng.random()
            g = int(120 + 120 * b)
            star[sy * W + sx] = (g, g, min(255, g + 25))

    gif = GifWriter(path, W, H, bytes(palette))
    tau = 2 * math.pi
    for fi in range(frames):
        phase = fi / frames                 # integer cycle counts => seamless loop
        draw_calls, lights = build_scene(
            cache, order, start_idx, dual,
            sax=tau * phase, say=tau * 2 * phase, saz=0.0,
            cam_pitch=0.30, cam_yaw=0.0,
            scene_yaw_extra=tau * phase if dual else 0.0,
            hue_base=phase, light_angle=tau * phase, offset=offset)
        zbuf, br, cr, cg, cb = rasterize(
            draw_calls, W, H, scale, cx, cy, cam, 1.0, ambient, lights)

        idxbuf = bytearray(W * H)
        for p in range(W * H):
            if zbuf[p] > 0.0:
                r, g, b = cr[p], cg[p], cb[p]
            else:
                sc = star.get(p)
                if sc is not None:
                    r, g, b = sc
                else:
                    yy = (p // W) / H        # subtle navy vertical gradient
                    r = int(6 + 4 * (1 - yy))
                    g = int(8 + 6 * (1 - yy))
                    b = int(20 + 16 * (1 - yy))
            idxbuf[p] = lut[r] * 36 + lut[g] * 6 + lut[b]
        gif.add_frame(idxbuf, delay)
        sys.stdout.write(f"\raurora3d: encoding GIF {fi + 1}/{frames} frames")
        sys.stdout.flush()
    gif.finish()
    print(f"\raurora3d: wrote {path}  ({W}x{H}, {frames} frames, "
          f"{'two shapes' if dual else order[start_idx]})           ")
    return 0


# --------------------------------------------------------------------------- #
#  Main loop
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description="Aurora3D terminal 3D engine.")
    parser.add_argument("--shape", choices=list(("torus", "sphere", "cube", "ripple")),
                        default="torus")
    parser.add_argument("--obj", help="path to a Wavefront .obj model to render")
    parser.add_argument("--text", help="render this string as chunky 3D letters")
    parser.add_argument("--points", type=int, default=26000,
                        help="target sample count for --obj models")
    parser.add_argument("--dual", action="store_true",
                        help="render two shapes at once, orbiting each other")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--no-stars", action="store_true")
    parser.add_argument("--frames", type=int, default=0,
                        help="render N frames then exit (0 = forever)")
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--gif", metavar="PATH",
                        help="render a seamless looping GIF to PATH and exit")
    parser.add_argument("--gif-frames", type=int, default=60)
    parser.add_argument("--gif-size", default="320x200", help="GIF size, WxH")
    parser.add_argument("--gif-fps", type=float, default=20.0)
    args = parser.parse_args(argv)

    enable_ansi()

    builtins = {
        "torus": make_torus, "sphere": make_sphere,
        "cube": make_cube, "ripple": make_ripple,
    }
    order = ["torus", "sphere", "cube", "ripple"]
    cache = {}

    # Optional generated shapes go to the front and become the default.
    if args.text:
        cache["text"] = make_text(args.text)
        order.insert(0, "text")
    if args.obj:
        try:
            cache["model"] = load_obj(args.obj, args.points)
        except (OSError, ValueError) as e:
            print(f"aurora3d: could not load model: {e}", file=sys.stderr)
            return 1
        order.insert(0, "model")

    start = "model" if args.obj else ("text" if args.text else args.shape)
    shape_idx = order.index(start)
    if start not in cache:
        cache[start] = builtins[start]()

    dual = args.dual

    def ensure(name):
        if name not in cache:
            cache[name] = builtins[name]()

    if dual:
        ensure(order[(shape_idx + 1) % len(order)])

    # ---- one-shot GIF export, then exit ----
    if args.gif:
        enable_ansi()
        return export_gif(args.gif, order, cache, shape_idx, dual, args)

    width, height = term_size()
    color = not args.no_color
    stars = not args.no_stars
    renderer = Renderer(width, height, color=color, stars=stars)

    spin = 1.0
    paused = False
    color_cycle = True
    light_orbits = True
    ax = ay = az = 0.0
    cam_yaw = cam_pitch = 0.0
    light_angle = 0.0
    hue_base = 0.0
    pair_angle = 0.0
    renderer.cam = 9.5 if dual else 7.0

    frame = 0
    fps_smoothed = 0.0
    t0 = time.time()
    last = t0
    budget = 1.0 / max(1.0, args.fps)

    hide_cursor()
    clear()

    try:
        with Keyboard() as kb:
            while True:
                now = time.time()
                dt = now - last
                last = now
                t = now - t0

                nw, nh = term_size()
                if (nw, nh) != (width, height):
                    width, height = nw, nh
                    renderer = Renderer(width, height, color=color, stars=stars)
                    renderer.cam = 9.5 if dual else 7.0
                    clear()

                key = kb.get()
                if key:
                    k = key.lower()
                    if k in ("q", "\x03"):
                        break
                    elif k in (" ", "n"):
                        shape_idx = (shape_idx + 1) % len(order)
                        name = order[shape_idx]
                        if name not in cache:
                            cache[name] = builtins[name]()
                    elif k == "c":
                        color_cycle = not color_cycle
                    elif k == "l":
                        light_orbits = not light_orbits
                    elif k == "s":
                        stars = not stars
                        renderer.stars = stars
                        renderer._make_stars()
                    elif k == "p":
                        paused = not paused
                    elif k == "r":
                        cam_yaw = cam_pitch = 0.0
                    elif k == "a":
                        cam_yaw -= 0.15
                    elif k == "d":
                        cam_yaw += 0.15
                    elif k == "w":
                        cam_pitch -= 0.15
                    elif k == "x":
                        cam_pitch += 0.15
                    elif k in ("+", "="):
                        spin = min(4.0, spin + 0.25)
                    elif k in ("-", "_"):
                        spin = max(0.0, spin - 0.25)
                    elif k == "2":
                        dual = not dual
                        renderer.cam = 9.5 if dual else 7.0
                        if dual:
                            ensure(order[(shape_idx + 1) % len(order)])
                        clear()

                if not paused:
                    ax += dt * 0.7 * spin
                    ay += dt * 1.1 * spin
                    az += dt * 0.3 * spin
                    if dual:
                        pair_angle += dt * 0.6
                if light_orbits:
                    light_angle += dt * 1.3
                if color_cycle:
                    hue_base = (hue_base + dt * 0.08) % 1.0

                if dual:
                    ensure(order[(shape_idx + 1) % len(order)])
                draw_calls, lights = build_scene(
                    cache, order, shape_idx, dual,
                    sax=ax, say=ay, saz=az,
                    cam_pitch=cam_pitch, cam_yaw=cam_yaw,
                    scene_yaw_extra=pair_angle if dual else 0.0,
                    hue_base=hue_base, light_angle=light_angle, offset=3.2)
                canvas = renderer.render(draw_calls, lights, t)

                inst = 1.0 / dt if dt > 0 else 0.0
                fps_smoothed = fps_smoothed * 0.9 + inst * 0.1

                name = order[shape_idx]
                if dual:
                    name = f"{name}+{order[(shape_idx + 1) % len(order)]}"
                hud = (
                    f"{ESC}[0m aurora3d | {name:<13} spin x{spin:.2f}"
                    f"{' (paused)' if paused else ''} fps {fps_smoothed:5.1f} | "
                    f"[space]shape [2]dual [wasx]cam [c]olor [l]ight [+/-]speed [q]uit"
                )[: width + 24]

                home()
                sys.stdout.write(canvas + "\n" + hud)
                sys.stdout.flush()

                frame += 1
                if args.frames and frame >= args.frames:
                    break

                elapsed = time.time() - now
                if elapsed < budget:
                    time.sleep(budget - elapsed)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(f"{ESC}[0m")
        show_cursor()
        clear()
        el = time.time() - t0
        avg = frame / el if el else 0.0
        print(f"aurora3d - rendered {frame} frames in {el:.1f}s "
              f"(avg {avg:.1f} fps). thanks for watching.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
