"""Spatial index over the triangles of a stock mesh (pure Python, no numpy).

Built once per in-process stock from the STL file (setup-frame millimetres):

* triangle vertices in one flat ``array('f')`` (9 values per triangle),
* a uniform grid (spatial hash) that maps a cell key to the triangle indices
  whose bounding box touches the cell.

Neighbourhood queries (``triangles_near``) touch only the cells that overlap
the query sphere, so a hover costs O(cells + local triangles) regardless of
mesh size. Normals and areas are computed lazily per triangle and cached.
Building is O(N) and is meant to run in a background thread (it does not use
the Fusion API).
"""
from __future__ import annotations

import math
import struct
import time
from array import array
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


class MeshIndex:
    """Triangle soup plus a uniform grid for local queries."""

    def __init__(self, coords: array, cell_size: float) -> None:
        self.coords = coords                      # 9 float32 per triangle
        self.triangle_count = len(coords) // 9
        self.cell_size = float(cell_size)
        self.grid: Dict[int, List[int]] = {}
        self._origin_cell = (0, 0, 0)
        self._span = 1
        self._normal_cache: Dict[int, Tuple[Vec3, float]] = {}
        self.build_seconds = 0.0
        self.bbox_min: Vec3 = (0.0, 0.0, 0.0)
        self.bbox_max: Vec3 = (0.0, 0.0, 0.0)
        self._build()

    # ---------------------------------------------------------------- build
    @classmethod
    def from_stl(cls, path: str, scale: float = 1.0, cell_size: Optional[float] = None) -> 'MeshIndex':
        """Read a binary or ASCII STL. Coordinates are multiplied by ``scale``."""
        with open(path, 'rb') as fh:
            data = fh.read()
        coords = array('f')
        if data[:5].lower() == b'solid' and b'facet' in data[:4096]:
            for line in data.decode('ascii', errors='ignore').splitlines():
                s = line.strip()
                if s.startswith('vertex'):
                    parts = s.split()
                    coords.extend((float(parts[1]) * scale, float(parts[2]) * scale, float(parts[3]) * scale))
        else:
            count = struct.unpack('<I', data[80:84])[0]
            if scale == 1.0:
                for tri in struct.iter_unpack('<12fH', data[84:84 + count * 50]):
                    coords.extend(tri[3:12])
            else:
                for tri in struct.iter_unpack('<12fH', data[84:84 + count * 50]):
                    coords.extend(c * scale for c in tri[3:12])
        return cls.from_coords(coords, cell_size)

    @classmethod
    def from_coords(cls, coords: Sequence[float], cell_size: Optional[float] = None) -> 'MeshIndex':
        arr = coords if isinstance(coords, array) and coords.typecode == 'f' else array('f', coords)
        if cell_size is None:
            cell_size = _pick_cell_size(arr)
        return cls(arr, cell_size)

    def _build(self) -> None:
        """O(N) grid build: C-level array slicing per chunk, integer cell keys."""
        t0 = time.perf_counter()
        c = self.coords
        n = self.triangle_count
        if n == 0:
            return
        inv = 1.0 / self.cell_size
        floor = math.floor
        xs = (c[0::9], c[3::9], c[6::9])
        ys = (c[1::9], c[4::9], c[7::9])
        zs = (c[2::9], c[5::9], c[8::9])
        self.bbox_min = (min(map(min, xs)), min(map(min, ys)), min(map(min, zs)))
        self.bbox_max = (max(map(max, xs)), max(map(max, ys)), max(map(max, zs)))
        ox = floor(self.bbox_min[0] * inv) - 1
        oy = floor(self.bbox_min[1] * inv) - 1
        oz = floor(self.bbox_min[2] * inv) - 1
        self._origin_cell = (ox, oy, oz)
        span = max(floor(self.bbox_max[0] * inv) - ox, floor(self.bbox_max[1] * inv) - oy,
                   floor(self.bbox_max[2] * inv) - oz) + 3
        self._span = span
        span2 = span * span
        grid: Dict[int, List[int]] = {}
        get = grid.get
        chunk = 65536   # bounds the temporary per-chunk lists (peak memory)
        for start in range(0, n, chunk):
            stop = min(n, start + chunk)
            sx = [a[start:stop] for a in xs]
            sy = [a[start:stop] for a in ys]
            sz = [a[start:stop] for a in zs]
            ix0 = [floor(v * inv) - ox for v in map(min, *sx)]
            ix1 = [floor(v * inv) - ox for v in map(max, *sx)]
            iy0 = [floor(v * inv) - oy for v in map(min, *sy)]
            iy1 = [floor(v * inv) - oy for v in map(max, *sy)]
            iz0 = [floor(v * inv) - oz for v in map(min, *sz)]
            iz1 = [floor(v * inv) - oz for v in map(max, *sz)]
            t = start
            for a, b, cc, d, e, f in zip(ix0, ix1, iy0, iy1, iz0, iz1):
                if a == b and cc == d and e == f:
                    key = a + cc * span + e * span2
                    lst = get(key)
                    if lst is None:
                        grid[key] = [t]
                    else:
                        lst.append(t)
                else:
                    for ix in range(a, b + 1):
                        for iy in range(cc, d + 1):
                            base = ix + iy * span
                            for iz in range(e, f + 1):
                                key = base + iz * span2
                                lst = get(key)
                                if lst is None:
                                    grid[key] = [t]
                                else:
                                    lst.append(t)
                t += 1
        self.grid = grid
        self.build_seconds = time.perf_counter() - t0

    # ------------------------------------------------------------- persistence
    def to_state(self) -> dict:
        """Plain-data snapshot (arrays, dict, tuples only) for the on-disk cache."""
        return {'coords': self.coords, 'cell_size': self.cell_size, 'grid': self.grid,
                'origin_cell': self._origin_cell, 'span': self._span, 'bbox_min': self.bbox_min,
                'bbox_max': self.bbox_max, 'build_seconds': self.build_seconds}

    @classmethod
    def from_state(cls, state: dict) -> 'MeshIndex':
        obj = cls.__new__(cls)
        obj.coords = state['coords']
        obj.triangle_count = len(obj.coords) // 9
        obj.cell_size = state['cell_size']
        obj.grid = state['grid']
        obj._origin_cell = tuple(state['origin_cell'])
        obj._span = state['span']
        obj._normal_cache = {}
        obj.build_seconds = state['build_seconds']
        obj.bbox_min = tuple(state['bbox_min'])
        obj.bbox_max = tuple(state['bbox_max'])
        return obj

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state['_normal_cache'] = {}
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)

    def _cell_key(self, ix: int, iy: int, iz: int) -> int:
        return (ix - self._origin_cell[0]) + (iy - self._origin_cell[1]) * self._span + (iz - self._origin_cell[2]) * self._span * self._span

    # -------------------------------------------------------------- access
    def vertices(self, t: int) -> Tuple[Vec3, Vec3, Vec3]:
        b = t * 9
        c = self.coords
        return ((c[b], c[b + 1], c[b + 2]), (c[b + 3], c[b + 4], c[b + 5]), (c[b + 6], c[b + 7], c[b + 8]))

    def normal_and_area(self, t: int) -> Tuple[Vec3, float]:
        """Unit normal and area of triangle ``t`` (cached). Degenerate triangles give area 0."""
        cached = self._normal_cache.get(t)
        if cached is not None:
            return cached
        a, b, c = self.vertices(t)
        cr = _cross(_sub(b, a), _sub(c, a))
        length = _norm(cr)
        if length < 1e-12:
            result = ((0.0, 0.0, 0.0), 0.0)
        else:
            result = ((cr[0] / length, cr[1] / length, cr[2] / length), 0.5 * length)
        self._normal_cache[t] = result
        return result

    def centroid(self, t: int) -> Vec3:
        a, b, c = self.vertices(t)
        return ((a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0, (a[2] + b[2] + c[2]) / 3.0)

    # -------------------------------------------------------------- queries
    def triangles_near(self, p: Vec3, radius: float, limit: int = 4000) -> List[int]:
        """Indices of triangles whose surface lies within ``radius`` of ``p``."""
        inv = 1.0 / self.cell_size
        ix0, ix1 = math.floor((p[0] - radius) * inv), math.floor((p[0] + radius) * inv)
        iy0, iy1 = math.floor((p[1] - radius) * inv), math.floor((p[1] + radius) * inv)
        iz0, iz1 = math.floor((p[2] - radius) * inv), math.floor((p[2] + radius) * inv)
        seen = set()
        out: List[int] = []
        r2 = radius * radius
        grid = self.grid
        key = self._cell_key
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                for iz in range(iz0, iz1 + 1):
                    lst = grid.get(key(ix, iy, iz))
                    if not lst:
                        continue
                    for t in lst:
                        if t in seen:
                            continue
                        seen.add(t)
                        if self.distance_sq_to_triangle(p, t) <= r2:
                            out.append(t)
                            if len(out) >= limit:
                                return out
        return out

    def nearest_triangle(self, p: Vec3, radius: float) -> Optional[int]:
        best, best_d = None, radius * radius
        for t in self.triangles_near(p, radius):
            d = self.distance_sq_to_triangle(p, t)
            if d <= best_d:
                best, best_d = t, d
        return best

    def distance_sq_to_triangle(self, p: Vec3, t: int) -> float:
        """Squared distance from ``p`` to triangle ``t`` (Ericson, Real-Time Collision Detection)."""
        a, b, c = self.vertices(t)
        ab = _sub(b, a)
        ac = _sub(c, a)
        ap = _sub(p, a)
        d1 = _dot(ab, ap)
        d2 = _dot(ac, ap)
        if d1 <= 0.0 and d2 <= 0.0:
            return _dot(ap, ap)
        bp = _sub(p, b)
        d3 = _dot(ab, bp)
        d4 = _dot(ac, bp)
        if d3 >= 0.0 and d4 <= d3:
            return _dot(bp, bp)
        vc = d1 * d4 - d3 * d2
        if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
            v = d1 / (d1 - d3) if (d1 - d3) != 0 else 0.0
            q = (a[0] + ab[0] * v, a[1] + ab[1] * v, a[2] + ab[2] * v)
            d = _sub(p, q)
            return _dot(d, d)
        cp = _sub(p, c)
        d5 = _dot(ab, cp)
        d6 = _dot(ac, cp)
        if d6 >= 0.0 and d5 <= d6:
            return _dot(cp, cp)
        vb = d5 * d2 - d1 * d6
        if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
            w = d2 / (d2 - d6) if (d2 - d6) != 0 else 0.0
            q = (a[0] + ac[0] * w, a[1] + ac[1] * w, a[2] + ac[2] * w)
            d = _sub(p, q)
            return _dot(d, d)
        va = d3 * d6 - d5 * d4
        if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
            denom = (d4 - d3) + (d5 - d6)
            w = (d4 - d3) / denom if denom != 0 else 0.0
            q = (b[0] + (c[0] - b[0]) * w, b[1] + (c[1] - b[1]) * w, b[2] + (c[2] - b[2]) * w)
            d = _sub(p, q)
            return _dot(d, d)
        denom = va + vb + vc
        if denom == 0:
            return _dot(ap, ap)
        v = vb / denom
        w = vc / denom
        q = (a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w, a[2] + ab[2] * v + ac[2] * w)
        d = _sub(p, q)
        return _dot(d, d)

    # -------------------------------------------------------------- ray cast
    def raycast(self, origin: Vec3, direction: Vec3, t_max: float = 1e9) -> Optional[Tuple[float, int, Vec3]]:
        """First triangle hit by the ray (Amanatides-Woo traversal of the occupied cells).

        Returns ``(t, triangle, point)`` or None. Cells are visited front to back
        and only their triangles are tested; traversal stops as soon as the best
        hit lies before the exit of the current cell, so the cost is the empty
        cells crossed plus a few occupied cells, independent of mesh size.
        """
        d = direction
        ln = _norm(d)
        if ln < 1e-12:
            return None
        d = (d[0] / ln, d[1] / ln, d[2] / ln)
        # clip against the bounding box (slab test)
        t_enter, t_exit = 0.0, t_max
        for axis in range(3):
            lo, hi = self.bbox_min[axis] - self.cell_size, self.bbox_max[axis] + self.cell_size
            if abs(d[axis]) < 1e-12:
                if origin[axis] < lo or origin[axis] > hi:
                    return None
                continue
            inv_d = 1.0 / d[axis]
            ta, tb = (lo - origin[axis]) * inv_d, (hi - origin[axis]) * inv_d
            if ta > tb:
                ta, tb = tb, ta
            if ta > t_enter:
                t_enter = ta
            if tb < t_exit:
                t_exit = tb
            if t_enter > t_exit:
                return None
        cell = self.cell_size
        inv = 1.0 / cell
        floor = math.floor
        p = (origin[0] + d[0] * t_enter, origin[1] + d[1] * t_enter, origin[2] + d[2] * t_enter)
        ix, iy, iz = floor(p[0] * inv), floor(p[1] * inv), floor(p[2] * inv)
        step = [1 if d[a] > 0 else -1 for a in range(3)]
        t_next = [0.0, 0.0, 0.0]
        t_delta = [0.0, 0.0, 0.0]
        idx = [ix, iy, iz]
        for a in range(3):
            if abs(d[a]) < 1e-12:
                t_next[a] = float('inf')
                t_delta[a] = float('inf')
            else:
                boundary = (idx[a] + (1 if step[a] > 0 else 0)) * cell
                t_next[a] = t_enter + (boundary - p[a]) / d[a]
                t_delta[a] = cell / abs(d[a])
        grid = self.grid
        key = self._cell_key
        best_t, best_tri = t_exit, -1
        span = self._span
        ox, oy, oz = self._origin_cell
        guard = 0
        while guard < 100000:
            guard += 1
            kx, ky, kz = idx[0] - ox, idx[1] - oy, idx[2] - oz
            if 0 <= kx < span and 0 <= ky < span and 0 <= kz < span:
                lst = grid.get(kx + ky * span + kz * span * span)
                if lst:
                    for t in lst:
                        th = self._ray_triangle(origin, d, t)
                        if th is not None and 0.0 <= th < best_t:
                            best_t, best_tri = th, t
            t_cell_exit = min(t_next)
            if best_tri >= 0 and best_t <= t_cell_exit:
                break
            if t_cell_exit > t_exit:
                break
            a = 0 if t_next[0] <= t_next[1] and t_next[0] <= t_next[2] else (1 if t_next[1] <= t_next[2] else 2)
            idx[a] += step[a]
            t_next[a] += t_delta[a]
        if best_tri < 0:
            return None
        return best_t, best_tri, (origin[0] + d[0] * best_t, origin[1] + d[1] * best_t, origin[2] + d[2] * best_t)

    def _ray_triangle(self, o: Vec3, d: Vec3, t: int) -> Optional[float]:
        """Moller-Trumbore; returns the ray parameter or None (both facing directions)."""
        c = self.coords
        b = t * 9
        ax, ay, az = c[b], c[b + 1], c[b + 2]
        e1x, e1y, e1z = c[b + 3] - ax, c[b + 4] - ay, c[b + 5] - az
        e2x, e2y, e2z = c[b + 6] - ax, c[b + 7] - ay, c[b + 8] - az
        px, py, pz = d[1] * e2z - d[2] * e2y, d[2] * e2x - d[0] * e2z, d[0] * e2y - d[1] * e2x
        det = e1x * px + e1y * py + e1z * pz
        if -1e-12 < det < 1e-12:
            return None
        inv = 1.0 / det
        tx, ty, tz = o[0] - ax, o[1] - ay, o[2] - az
        u = (tx * px + ty * py + tz * pz) * inv
        if u < -1e-9 or u > 1.0 + 1e-9:
            return None
        qx, qy, qz = ty * e1z - tz * e1y, tz * e1x - tx * e1z, tx * e1y - ty * e1x
        v = (d[0] * qx + d[1] * qy + d[2] * qz) * inv
        if v < -1e-9 or u + v > 1.0 + 1e-9:
            return None
        return (e2x * qx + e2y * qy + e2z * qz) * inv

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        sizes = [len(v) for v in self.grid.values()]
        return {
            'triangles': self.triangle_count,
            'cells': len(self.grid),
            'cell_size_mm': self.cell_size,
            'mean_triangles_per_cell': (sum(sizes) / len(sizes)) if sizes else 0.0,
            'max_triangles_per_cell': max(sizes) if sizes else 0,
            'build_seconds': self.build_seconds,
        }


def _pick_cell_size(coords: array) -> float:
    """A cell size that keeps roughly ten triangles per occupied cell."""
    n = len(coords) // 9
    if n == 0:
        return 1.0
    mins = [min(coords[i::3]) for i in range(3)]
    maxs = [max(coords[i::3]) for i in range(3)]
    ext = [max(maxs[i] - mins[i], 1e-6) for i in range(3)]
    # Assume triangles cover the surface of the bounding box; ~16 triangles per cell.
    surface = 2.0 * (ext[0] * ext[1] + ext[1] * ext[2] + ext[0] * ext[2])
    cell = math.sqrt(surface / max(n / 16.0, 1.0))
    return max(0.05, min(cell, max(ext) / 4.0))
