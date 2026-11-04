"""Generate original solar-panel PNG icons using only the Python standard library."""

from pathlib import Path
import math
import struct
import zlib


def pixel(x: float, y: float) -> tuple[int, int, int, int]:
    """Draw a blue photovoltaic panel and golden sun on a transparent canvas."""
    blue = (24, 77, 116, 255)
    gold = (245, 177, 36, 255)
    if math.hypot(x - 191, y - 48) <= 30:
        return gold
    for angle in range(0, 360, 45):
        dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        along = (x - 191) * dx + (y - 48) * dy
        across = abs((x - 191) * dy - (y - 48) * dx)
        if 38 <= along <= 48 and across <= 4:
            return gold
    if 224 <= y <= 236 and 48 <= x <= 208:
        return blue
    if 198 <= y <= 224 and (86 <= x <= 98 or 158 <= x <= 170):
        return blue
    if 96 <= y <= 198:
        left = 24 - (y - 96) * 24 / 102
        right = 232 + (y - 96) * 24 / 102
        if left <= x <= right:
            u = (x - left) / (right - left)
            v = (y - 96) / 102
            if min(u, 1 - u) < 0.025 or min(v, 1 - v) < 0.05:
                return blue
            if any(abs(u - line / 4) < 0.009 for line in (1, 2, 3)) or abs(v - 0.5) < 0.018:
                return (189, 229, 240, 255)
            return (37, 139, 179, 255)
    return (0, 0, 0, 0)


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def render(size: int) -> bytes:
    """Supersample edges and encode an RGBA PNG."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG scanline filter: none.
        for x in range(size):
            samples = [
                pixel((x + dx) * 256 / size, (y + dy) * 256 / size)
                for dx in (0.25, 0.75)
                for dy in (0.25, 0.75)
            ]
            opaque = [color for color in samples if color[3]]
            if opaque:
                rows.extend(round(sum(color[c] for color in opaque) / len(opaque)) for c in range(3))
                rows.append(round(255 * len(opaque) / len(samples)))
            else:
                rows.extend((0, 0, 0, 0))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "custom_components/plenitude_panels/brand"
    if not destination.is_dir():
        raise SystemExit(f"Brand directory does not exist: {destination}")
    for filename, size in (("icon.png", 256), ("icon@2x.png", 512)):
        (destination / filename).write_bytes(render(size))
        print(f"Generated {filename} ({size}x{size})")
