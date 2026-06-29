"""
generate_icons.py — produce the PNG launcher icons for the S.A.M.S. teacher PWA.

PWA installability on Android requires raster PNG icons (192 & 512). This script
draws them with the standard library only (zlib + struct) — no Pillow/numpy — so
it runs anywhere the backend's Python runs. The mark mirrors icon.svg: an amber
microphone on the app's navy background.

Run:  python generate_icons.py
"""
import struct
import zlib
from pathlib import Path

# Palette (matches the dashboard / manifest theme)
BG    = (13, 17, 23)     # #0d1117 navy
AMBER = (240, 165, 0)    # #f0a500


def _png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode raw RGBA pixel bytes into a PNG byte string."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    # Prepend the per-scanline filter byte (0 = none) to each row.
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(pixels[y * stride:(y + 1) * stride])

    sig    = b"\x89PNG\r\n\x1a\n"
    ihdr   = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat   = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _render(size: int) -> bytes:
    """Draw the icon at the given square size and return RGBA bytes."""
    s = size
    buf = bytearray(s * s * 4)

    def put(x: int, y: int, rgb):
        if 0 <= x < s and 0 <= y < s:
            i = (y * s + x) * 4
            buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = rgb[0], rgb[1], rgb[2], 255

    # Background fill.
    for y in range(s):
        for x in range(s):
            put(x, y, BG)

    cx = s / 2.0
    unit = s / 512.0  # design coordinates are based on a 512px canvas

    # Microphone capsule: rounded vertical bar centred horizontally.
    cap_w = 96 * unit
    cap_top, cap_bot = 120 * unit, 296 * unit
    cap_r = cap_w / 2.0
    left, right = cx - cap_r, cx + cap_r
    for y in range(int(cap_top), int(cap_bot)):
        for x in range(int(left), int(right)):
            # round the top & bottom ends
            if y < cap_top + cap_r:
                if (x - cx) ** 2 + (y - (cap_top + cap_r)) ** 2 > cap_r ** 2:
                    continue
            elif y > cap_bot - cap_r:
                if (x - cx) ** 2 + (y - (cap_bot - cap_r)) ** 2 > cap_r ** 2:
                    continue
            put(x, y, AMBER)

    # Pickup arc (half-ring) under the capsule.
    arc_cy = 264 * unit
    r_out, r_in = 96 * unit, 68 * unit
    for y in range(int(arc_cy - r_out), int(arc_cy + r_out)):
        for x in range(int(cx - r_out), int(cx + r_out)):
            d2 = (x - cx) ** 2 + (y - arc_cy) ** 2
            if r_in ** 2 <= d2 <= r_out ** 2 and y >= arc_cy:
                put(x, y, AMBER)

    # Stand: vertical stem + base bar.
    stem_w = 26 * unit
    for y in range(int(360 * unit), int(410 * unit)):
        for x in range(int(cx - stem_w / 2), int(cx + stem_w / 2)):
            put(x, y, AMBER)
    base_half = 52 * unit
    for y in range(int(408 * unit), int(408 * unit + stem_w)):
        for x in range(int(cx - base_half), int(cx + base_half)):
            put(x, y, AMBER)

    return bytes(buf)


def main():
    out = Path(__file__).resolve().parent
    for size in (192, 512):
        png = _png(size, size, _render(size))
        (out / f"icon-{size}.png").write_bytes(png)
        print(f"wrote icon-{size}.png ({len(png)} bytes)")


if __name__ == "__main__":
    main()
