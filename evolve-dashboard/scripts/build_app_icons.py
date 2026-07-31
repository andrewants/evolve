#!/usr/bin/env python3
"""Rasterise the Momentum mark into the home-screen icons.

iOS uses `apple-touch-icon` for the installed app's tile and refuses SVG
there, so the favicon's vector mark is scanned out to PNG. Both shapes in it
are simple distance tests, which keeps this stdlib-only like the rest of the
add-on — no Pillow, no build-time toolchain.

Usage:
    python3 scripts/build_app_icons.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "app" / "www" / "icons"

BG = (0x16, 0x18, 0x26)
FG = (0x91, 0x84, 0xD9)

# Straight from the favicon in index.html, in its 32x32 viewBox.
VIEWBOX = 32.0
CORNER_RADIUS = 8.0
STROKE_WIDTH = 3.0
CHECK_POINTS = [(7.0, 22.0), (12.0, 15.0), (16.0, 19.0), (25.0, 8.0)]

SUPERSAMPLE = 4
# Android masks maskable icons down to a circle inscribed in the middle 80%,
# so that variant shrinks the mark and bleeds the background to every edge.
MASKABLE_INSET = 0.22


def _inside_rounded_rect(x: float, y: float) -> bool:
    r = CORNER_RADIUS
    cx = min(max(x, r), VIEWBOX - r)
    cy = min(max(y, r), VIEWBOX - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _distance_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _inside_check(x: float, y: float) -> bool:
    """Round caps and joins fall straight out of a distance-to-segment test."""
    half = STROKE_WIDTH / 2.0
    return any(
        _distance_to_segment(x, y, ax, ay, bx, by) <= half
        for (ax, ay), (bx, by) in zip(CHECK_POINTS, CHECK_POINTS[1:])
    )


def render(size: int, maskable: bool = False) -> list[bytes]:
    scale = VIEWBOX / size
    inset = MASKABLE_INSET if maskable else 0.0
    samples = SUPERSAMPLE * SUPERSAMPLE
    rows: list[bytes] = []
    for pixel_y in range(size):
        row = bytearray()
        for pixel_x in range(size):
            r = g = b = 0
            for sub_y in range(SUPERSAMPLE):
                for sub_x in range(SUPERSAMPLE):
                    x = (pixel_x + (sub_x + 0.5) / SUPERSAMPLE) * scale
                    y = (pixel_y + (sub_y + 0.5) / SUPERSAMPLE) * scale
                    if maskable:
                        # Background bleeds to the edge; only the mark shrinks.
                        mark_x = VIEWBOX / 2 + (x - VIEWBOX / 2) / (1.0 - inset)
                        mark_y = VIEWBOX / 2 + (y - VIEWBOX / 2) / (1.0 - inset)
                    else:
                        # Corners stay square: iOS applies its own mask, and a
                        # transparent corner would show through as black.
                        mark_x, mark_y = x, y
                        if not _inside_rounded_rect(x, y):
                            r, g, b = r + BG[0], g + BG[1], b + BG[2]
                            continue
                    colour = FG if _inside_check(mark_x, mark_y) else BG
                    r, g, b = r + colour[0], g + colour[1], b + colour[2]
            row += bytes((r // samples, g // samples, b // samples))
        rows.append(bytes(row))
    return rows


def write_png(path: Path, rows: list[bytes]) -> None:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    size = len(rows)
    raw = b"".join(b"\x00" + row for row in rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)
    print(f"Wrote {path.relative_to(ROOT)} ({size}x{size}, {len(png)} bytes)")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_png(OUT_DIR / "icon-180.png", render(180))  # apple-touch-icon
    write_png(OUT_DIR / "icon-192.png", render(192))
    write_png(OUT_DIR / "icon-512.png", render(512))
    write_png(OUT_DIR / "icon-maskable-512.png", render(512, maskable=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
