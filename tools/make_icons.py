"""Generate the add-in's toolbar icons (16/32/64 px PNG) without external packages.

The icon is a machined stock block with a crosshair marker on it: a light block
with a darker pocket, and an orange target dot. Run from the repository root:

    python tools/make_icons.py
"""
import os
import struct
import zlib

OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'FusionIPWInspector', 'resources', 'ipw_inspector')

BLOCK = (176, 186, 196, 255)      # stock grey
BLOCK_DARK = (120, 130, 142, 255)  # pocket / shadow
ACCENT = (255, 122, 0, 255)       # Fusion-ish orange
RING = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)


def write_png(path, size, pixels):
    raw = b''.join(b'\x00' + b''.join(struct.pack('4B', *px) for px in row) for row in pixels)

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 9))
    png += chunk(b'IEND', b'')
    with open(path, 'wb') as fh:
        fh.write(png)


def blend(dst, src):
    a = src[3] / 255.0
    return tuple(int(round(src[i] * a + dst[i] * (1 - a))) for i in range(3)) + (max(dst[3], src[3]),)


def draw(size):
    s = size
    img = [[CLEAR for _ in range(s)] for _ in range(s)]
    m = max(1, s // 16)                  # margin
    # block body
    for y in range(m + s // 5, s - m):
        for x in range(m, s - m):
            img[y][x] = BLOCK
    # top face (lighter band) to suggest a 3D block
    for y in range(m, m + s // 5):
        for x in range(m + s // 8, s - m - s // 8):
            img[y][x] = (206, 214, 222, 255)
    # a machined pocket on the block's front
    px0, px1 = m + s // 4, s - m - s // 4
    py0, py1 = m + s // 5 + s // 6, s - m - s // 6
    for y in range(py0, py1):
        for x in range(px0, px1):
            img[y][x] = BLOCK_DARK
    # crosshair target centred on the pocket
    cx, cy = (px0 + px1) / 2.0 - 0.5, (py0 + py1) / 2.0 - 0.5
    r_outer = max(2.0, s * 0.22)
    r_inner = max(1.0, s * 0.09)
    for y in range(s):
        for x in range(s):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if abs(d - r_outer) <= max(0.7, s / 32.0):
                img[y][x] = blend(img[y][x], RING)
            if d <= r_inner:
                img[y][x] = ACCENT
    # crosshair ticks
    t = max(1, s // 16)
    for k in range(int(r_outer + s * 0.12)):
        for off in range(-t // 2, t // 2 + 1):
            for (dx, dy) in ((k, off), (-k, off), (off, k), (off, -k)):
                x, y = int(round(cx + dx)), int(round(cy + dy))
                if 0 <= x < s and 0 <= y < s and abs(k) > r_inner + 1:
                    img[y][x] = blend(img[y][x], (255, 255, 255, 200))
    return img


def draw_crosshair(size, colour=ACCENT):
    """Screen-space marker for the picked point: ring + cross, transparent background."""
    s = size
    img = [[CLEAR for _ in range(s)] for _ in range(s)]
    c = (s - 1) / 2.0
    r_ring = s * 0.32
    for y in range(s):
        for x in range(s):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            on_ring = abs(d - r_ring) <= 1.0
            on_cross = (abs(x - c) <= 0.6 or abs(y - c) <= 0.6) and d <= s * 0.48 and d >= s * 0.12
            centre = d <= 1.3
            if on_ring or on_cross or centre:
                img[y][x] = colour
            elif abs(d - r_ring) <= 1.9 or ((abs(x - c) <= 1.4 or abs(y - c) <= 1.4) and s * 0.12 <= d <= s * 0.48):
                img[y][x] = (0, 0, 0, 140)   # thin dark halo for contrast on light stock
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (16, 32, 64):
        write_png(os.path.join(OUT_DIR, '%dx%d.png' % (size, size)), size, draw(size))
    marker_dir = os.path.join(os.path.dirname(OUT_DIR), 'marker')
    os.makedirs(marker_dir, exist_ok=True)
    write_png(os.path.join(marker_dir, 'crosshair.png'), 32, draw_crosshair(32))
    write_png(os.path.join(marker_dir, 'preview.png'), 24, draw_crosshair(24, (0, 190, 255, 255)))
    print('icons written to', os.path.abspath(OUT_DIR), 'and', os.path.abspath(marker_dir))


if __name__ == '__main__':
    main()
