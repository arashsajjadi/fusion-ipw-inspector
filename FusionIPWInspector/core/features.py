"""Precomputed machining features of a stock mesh (pure Python, no numpy).

Built once per in-process stock, in a background thread, from a ``MeshIndex``:

1. every triangle's normal and area (one pass over the mesh);
2. per grid cell, the triangles are clustered into locally planar groups
   ("cell planes": normal within 10 degrees, offset within 0.05 mm);
3. cell planes of neighbouring cells that describe the same plane are merged
   with a union-find into global *patches* (area-weighted plane fit, RMS
   residual over all member vertices); a cell plane that drifted away from its
   patch's plane (curved surfaces) is split off again;
4. adjacent patches (present in neighbouring cells) at 20 degrees or more
   give *edges*: the intersection line clipped to where both patches really
   meet (runs of shared cells along the line);
5. an edge plus a third patch adjacent to both gives a *corner*: the
   well-conditioned three-plane intersection, validated against the cells
   where all three patches are present.

Features are stored in a coarse grid (a few millimetres per cell), so a hover
only inspects the coarse cells around the cursor: O(1) candidate retrieval
with a small bounded amount of arithmetic. Both corners of the same tab share
the same fitted planes, so their shared coordinates are identical by
construction.

Everything is O(N) once; nothing here touches the Fusion API.
"""
from __future__ import annotations

import math
import time
from array import array
from typing import Dict, List, Optional, Sequence, Tuple

from .mesh_index import MeshIndex, Vec3, _cross, _dot, _norm, _sub

CELL_CLUSTER_COS = math.cos(math.radians(10.0))   # triangles in one cell plane
CELL_OFFSET_TOL = 0.05                            # mm
MERGE_COS = math.cos(math.radians(4.0))           # cell planes of neighbouring cells -> one patch
MERGE_OFFSET_TOL = 0.06                           # mm, mutual offset consistency
SPLIT_COS = math.cos(math.radians(6.0))           # a cell plane this far from its patch is split off
SPLIT_OFFSET_TOL = 0.10                           # mm
GOOD_RMS = 0.05                                   # mm, patches usable for edges and corners
MIN_PATCH_AREA = 0.02                             # mm^2
EDGE_MIN_SIN = math.sin(math.radians(20.0))
CORNER_MIN_DET = 0.15
COARSE_MM = 3.0                                   # target size of a feature cell
MIN_FACE_WIDTH_CELLS = 3.0                        # a face must span this many cells across an edge


class Patch:
    __slots__ = ('id', 'normal', 'offset', 'point', 'area', 'rms', 'triangles', 'cells', 'good')

    def __init__(self, pid: int, normal: Vec3, point: Vec3, area: float, triangles: int) -> None:
        self.id = pid
        self.normal = normal
        self.point = point
        self.offset = _dot(normal, point)
        self.area = area
        self.rms = 0.0
        self.triangles = triangles
        self.cells: List[int] = []
        self.good = False

    def distance(self, p: Vec3) -> float:
        return _dot(self.normal, p) - self.offset

    def project(self, p: Vec3) -> Vec3:
        d = self.distance(p)
        n = self.normal
        return (p[0] - n[0] * d, p[1] - n[1] * d, p[2] - n[2] * d)


class EdgeFeature:
    __slots__ = ('id', 'a', 'b', 'x0', 'u', 't0', 't1', 'sin_angle', 'rms')

    def __init__(self, eid: int, a: int, b: int, x0: Vec3, u: Vec3, t0: float, t1: float, sin_angle: float, rms: float) -> None:
        self.id, self.a, self.b, self.x0, self.u, self.t0, self.t1, self.sin_angle, self.rms = eid, a, b, x0, u, t0, t1, sin_angle, rms

    def closest(self, p: Vec3) -> Tuple[Vec3, float]:
        """Closest point of the segment to ``p`` and the parameter used."""
        t = _dot(_sub(p, self.x0), self.u)
        t = self.t0 if t < self.t0 else (self.t1 if t > self.t1 else t)
        return (self.x0[0] + self.u[0] * t, self.x0[1] + self.u[1] * t, self.x0[2] + self.u[2] * t), t


class CornerFeature:
    __slots__ = ('id', 'patches', 'point', 'det', 'rms')

    def __init__(self, cid: int, patches: Tuple[int, int, int], point: Vec3, det: float, rms: float) -> None:
        self.id, self.patches, self.point, self.det, self.rms = cid, patches, point, det, rms


class FeatureGraph:
    """Patches, edges and corners of one stock mesh plus coarse-cell lookup tables."""

    def __init__(self, index: MeshIndex) -> None:
        self.index = index
        self.patches: List[Patch] = []
        self.edges: List[EdgeFeature] = []
        self.corners: List[CornerFeature] = []
        self.tri_patch = array('i')            # patch id per triangle (-1: none)
        self.coarse = 1                        # fine cells per coarse cell (per axis)
        self.coarse_size = index.cell_size
        self.cell_patches: Dict[tuple, List[int]] = {}
        self.cell_edges: Dict[tuple, List[int]] = {}
        self.cell_corners: Dict[tuple, List[int]] = {}
        self.timings: Dict[str, float] = {}
        self.build_seconds = 0.0
        self._cell_cov: Dict[int, tuple] = {}

    # ----------------------------------------------------------------- build
    @classmethod
    def build(cls, index: MeshIndex) -> 'FeatureGraph':
        g = cls(index)
        g._build()
        return g

    def _build(self) -> None:
        t_start = time.perf_counter()
        idx = self.index
        n = idx.triangle_count
        c = idx.coords
        # 1. normals, areas and centroids of every triangle (array slices keep this C-fast per vertex)
        t0 = time.perf_counter()
        ax, ay, az = c[0::9], c[1::9], c[2::9]
        bx, by, bz = c[3::9], c[4::9], c[5::9]
        cx, cy, cz = c[6::9], c[7::9], c[8::9]
        nx, ny, nz, area = array('f'), array('f'), array('f'), array('f')
        sqrt = math.sqrt
        for a0, a1, a2, b0, b1, b2, c0, c1, c2 in zip(ax, ay, az, bx, by, bz, cx, cy, cz):
            ux, uy, uz = b0 - a0, b1 - a1, b2 - a2
            vx, vy, vz = c0 - a0, c1 - a1, c2 - a2
            px, py, pz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            ln = sqrt(px * px + py * py + pz * pz)
            if ln < 1e-12:
                nx.append(0.0); ny.append(0.0); nz.append(0.0); area.append(0.0)
            else:
                nx.append(px / ln); ny.append(py / ln); nz.append(pz / ln); area.append(0.5 * ln)
        self.timings['normals'] = time.perf_counter() - t0

        # 2. cell planes
        t0 = time.perf_counter()
        cp_cell: List[int] = []
        cp_n: List[Vec3] = []
        cp_c: List[Vec3] = []
        cp_area: List[float] = []
        cp_tris: List[List[int]] = []
        third = 1.0 / 3.0
        for key, tris in idx.grid.items():
            local: List[int] = []      # cell plane ids of this cell
            for t in tris:
                a = area[t]
                if a <= 1e-9:
                    continue
                b = t * 9
                cen = ((c[b] + c[b + 3] + c[b + 6]) * third, (c[b + 1] + c[b + 4] + c[b + 7]) * third,
                       (c[b + 2] + c[b + 5] + c[b + 8]) * third)
                tn = (nx[t], ny[t], nz[t])
                placed = -1
                for pid in local:
                    pn = cp_n[pid]
                    if pn[0] * tn[0] + pn[1] * tn[1] + pn[2] * tn[2] >= CELL_CLUSTER_COS:
                        pc = cp_c[pid]
                        if abs(pn[0] * (cen[0] - pc[0]) + pn[1] * (cen[1] - pc[1]) + pn[2] * (cen[2] - pc[2])) <= CELL_OFFSET_TOL:
                            placed = pid
                            break
                if placed < 0:
                    placed = len(cp_cell)
                    cp_cell.append(key); cp_n.append(tn); cp_c.append(cen); cp_area.append(a); cp_tris.append([t])
                    local.append(placed)
                else:
                    w = cp_area[placed] + a
                    pn, pc = cp_n[placed], cp_c[placed]
                    mn = ((pn[0] * cp_area[placed] + tn[0] * a) / w, (pn[1] * cp_area[placed] + tn[1] * a) / w,
                          (pn[2] * cp_area[placed] + tn[2] * a) / w)
                    ln = _norm(mn) or 1.0
                    cp_n[placed] = (mn[0] / ln, mn[1] / ln, mn[2] / ln)
                    cp_c[placed] = ((pc[0] * cp_area[placed] + cen[0] * a) / w, (pc[1] * cp_area[placed] + cen[1] * a) / w,
                                    (pc[2] * cp_area[placed] + cen[2] * a) / w)
                    cp_area[placed] = w
                    cp_tris[placed].append(t)
        m = len(cp_cell)
        self.timings['cell_planes'] = time.perf_counter() - t0

        # 3. merge cell planes of neighbouring cells (union-find)
        t0 = time.perf_counter()
        parent = list(range(m))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        by_cell: Dict[int, List[int]] = {}
        for pid, key in enumerate(cp_cell):
            by_cell.setdefault(key, []).append(pid)
        span = idx._span
        span2 = span * span
        forward = [(dx, dy, dz) for dz in (0, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                   if (dz, dy, dx) > (0, 0, 0)]
        for key, ids in by_cell.items():
            ix = key % span
            iy = (key // span) % span
            iz = key // span2
            for dx, dy, dz in forward:
                jx, jy, jz = ix + dx, iy + dy, iz + dz
                if jx < 0 or jy < 0 or jz < 0 or jx >= span or jy >= span or jz >= span:
                    continue
                other = by_cell.get(jx + jy * span + jz * span2)
                if not other:
                    continue
                for i in ids:
                    ni, ci = cp_n[i], cp_c[i]
                    di = ni[0] * ci[0] + ni[1] * ci[1] + ni[2] * ci[2]
                    for j in other:
                        nj = cp_n[j]
                        if ni[0] * nj[0] + ni[1] * nj[1] + ni[2] * nj[2] < MERGE_COS:
                            continue
                        cj = cp_c[j]
                        if abs(ni[0] * cj[0] + ni[1] * cj[1] + ni[2] * cj[2] - di) > MERGE_OFFSET_TOL:
                            continue
                        dj = nj[0] * cj[0] + nj[1] * cj[1] + nj[2] * cj[2]
                        if abs(nj[0] * ci[0] + nj[1] * ci[1] + nj[2] * ci[2] - dj) > MERGE_OFFSET_TOL:
                            continue
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            parent[ri] = rj
        self.timings['merge'] = time.perf_counter() - t0

        # 4. patches: aggregate, then split off cell planes that drifted from the patch plane
        t0 = time.perf_counter()
        root_of = [find(i) for i in range(m)]
        agg: Dict[int, list] = {}
        for i in range(m):
            r = root_of[i]
            a = cp_area[i]
            nn, cc = cp_n[i], cp_c[i]
            e = agg.get(r)
            if e is None:
                agg[r] = [nn[0] * a, nn[1] * a, nn[2] * a, cc[0] * a, cc[1] * a, cc[2] * a, a]
            else:
                e[0] += nn[0] * a; e[1] += nn[1] * a; e[2] += nn[2] * a
                e[3] += cc[0] * a; e[4] += cc[1] * a; e[5] += cc[2] * a; e[6] += a
        plane_of_root: Dict[int, Tuple[Vec3, float]] = {}
        for r, e in agg.items():
            a = e[6] or 1.0
            nn = (e[0], e[1], e[2])
            ln = _norm(nn) or 1.0
            nn = (nn[0] / ln, nn[1] / ln, nn[2] / ln)
            cc = (e[3] / a, e[4] / a, e[5] / a)
            plane_of_root[r] = (nn, _dot(nn, cc))
        patch_of_cp = [0] * m
        patches: List[Patch] = []
        patch_by_root: Dict[int, int] = {}
        for i in range(m):
            r = root_of[i]
            pn, pd = plane_of_root[r]
            ni, ci = cp_n[i], cp_c[i]
            if _dot(pn, ni) < SPLIT_COS or abs(_dot(pn, ci) - pd) > SPLIT_OFFSET_TOL:
                pid = len(patches)
                patches.append(Patch(pid, ni, ci, cp_area[i], len(cp_tris[i])))
            else:
                pid = patch_by_root.get(r)
                if pid is None:
                    pid = len(patches)
                    e = agg[r]
                    a = e[6] or 1.0
                    patches.append(Patch(pid, pn, (e[3] / a, e[4] / a, e[5] / a), e[6], 0))
                    patch_by_root[r] = pid
                patches[pid].triangles += len(cp_tris[i])
            patch_of_cp[i] = pid
            patches[pid].cells.append(cp_cell[i])
        # Final plane per patch from its unique triangles (a triangle that spans several cells
        # sits in several cell planes; it must count once), residual against the merged plane.
        k = len(patches)
        acc = [0.0] * k
        cnt = [0] * k
        snx, sny, snz, scx, scy, scz, sa = [0.0] * k, [0.0] * k, [0.0] * k, [0.0] * k, [0.0] * k, [0.0] * k, [0.0] * k
        tri_patch = array('i', [-1]) * n
        for i in range(m):
            pid = patch_of_cp[i]
            p = patches[pid]
            pn, pd = p.normal, p.offset
            s = 0.0
            cnt_i = 0
            for t in cp_tris[i]:
                if tri_patch[t] >= 0:
                    continue
                tri_patch[t] = pid
                a = area[t]
                b = t * 9
                x0, y0, z0 = c[b], c[b + 1], c[b + 2]
                x1, y1, z1 = c[b + 3], c[b + 4], c[b + 5]
                x2, y2, z2 = c[b + 6], c[b + 7], c[b + 8]
                snx[pid] += nx[t] * a; sny[pid] += ny[t] * a; snz[pid] += nz[t] * a
                scx[pid] += (x0 + x1 + x2) * third * a; scy[pid] += (y0 + y1 + y2) * third * a
                scz[pid] += (z0 + z1 + z2) * third * a; sa[pid] += a
                d0 = pn[0] * x0 + pn[1] * y0 + pn[2] * z0 - pd
                d1 = pn[0] * x1 + pn[1] * y1 + pn[2] * z1 - pd
                d2 = pn[0] * x2 + pn[1] * y2 + pn[2] * z2 - pd
                s += d0 * d0 + d1 * d1 + d2 * d2
                cnt_i += 3
            acc[pid] += s
            cnt[pid] += cnt_i
        for p in patches:
            pid = p.id
            if sa[pid] > 0.0:
                nn = (snx[pid], sny[pid], snz[pid])
                ln = _norm(nn) or 1.0
                p.normal = (nn[0] / ln, nn[1] / ln, nn[2] / ln)
                p.point = (scx[pid] / sa[pid], scy[pid] / sa[pid], scz[pid] / sa[pid])
                p.offset = _dot(p.normal, p.point)
                p.area = sa[pid]
                p.triangles = cnt[pid] // 3
        for p in patches:
            p.rms = math.sqrt(acc[p.id] / cnt[p.id]) if cnt[p.id] else 0.0
            p.good = p.rms <= GOOD_RMS and p.area >= MIN_PATCH_AREA
        self.patches = patches
        self.tri_patch = tri_patch
        self.timings['patches'] = time.perf_counter() - t0

        # 5. coarse cells and adjacency
        t0 = time.perf_counter()
        self.coarse = max(1, int(round(COARSE_MM / idx.cell_size)))
        self.coarse_size = idx.cell_size * self.coarse
        cell_patches: Dict[tuple, set] = {}
        fine_patches: Dict[int, List[int]] = {}
        for p in patches:
            seen = set()
            for key in p.cells:
                lst = fine_patches.get(key)
                if lst is None:
                    fine_patches[key] = [p.id]
                elif p.id not in lst:
                    lst.append(p.id)
                ck = self._coarse_key_of_fine(key)
                if ck not in seen:
                    seen.add(ck)
                    cell_patches.setdefault(ck, set()).add(p.id)
        self.cell_patches = {k: sorted(v) for k, v in cell_patches.items()}
        # adjacency: patches in neighbouring fine cells; remember the shared fine cells
        shared: Dict[Tuple[int, int], List[int]] = {}
        neighbours = [(dx, dy, dz) for dz in (-1, 0, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
        for key, ids in fine_patches.items():
            ix = key % span
            iy = (key // span) % span
            iz = key // span2
            around: set = set(ids)
            for dx, dy, dz in neighbours:
                jx, jy, jz = ix + dx, iy + dy, iz + dz
                if jx < 0 or jy < 0 or jz < 0 or jx >= span or jy >= span or jz >= span:
                    continue
                o = fine_patches.get(jx + jy * span + jz * span2)
                if o:
                    around.update(o)
            for a in ids:
                if not patches[a].good:
                    continue
                for b in around:
                    if b <= a or not patches[b].good:
                        continue
                    shared.setdefault((a, b), []).append(key)
        self.timings['adjacency'] = time.perf_counter() - t0

        # 6. edges
        t0 = time.perf_counter()
        edges: List[EdgeFeature] = []
        cell = idx.cell_size
        adj: Dict[int, set] = {}
        for (a, b), keys in shared.items():
            pa, pb = patches[a], patches[b]
            d = _cross(pa.normal, pb.normal)
            s = _norm(d)
            if s < EDGE_MIN_SIN:
                continue
            u = (d[0] / s, d[1] / s, d[2] / s)
            x0 = _line_point(pa, pb, u)
            if x0 is None:
                continue
            # Each patch must extend across the edge by a few cells: a thin strip of cell
            # planes on a fillet is not a machined face and must not produce edges.
            if self._width_across(pa, u) < MIN_FACE_WIDTH_CELLS * cell or self._width_across(pb, u) < MIN_FACE_WIDTH_CELLS * cell:
                continue
            ts = []
            for key in keys:
                cc = self._fine_center(key)
                w = _sub(cc, x0)
                t = _dot(w, u)
                off = _sub(w, (u[0] * t, u[1] * t, u[2] * t))
                if _norm(off) <= 1.8 * cell:
                    ts.append(t)
            if not ts:
                continue
            ts.sort()
            runs: List[Tuple[float, float]] = []
            start = prev = ts[0]
            for t in ts[1:]:
                if t - prev > 3.0 * cell:
                    runs.append((start, prev))
                    start = t
                prev = t
            runs.append((start, prev))
            rms = max(pa.rms, pb.rms)
            for r0, r1 in runs:
                eid = len(edges)
                edges.append(EdgeFeature(eid, a, b, x0, u, r0 - cell, r1 + cell, s, rms))
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        self.edges = edges
        cell_edges: Dict[tuple, set] = {}
        for e in edges:
            step = self.coarse_size * 0.5
            t = e.t0
            while True:
                p = (e.x0[0] + e.u[0] * t, e.x0[1] + e.u[1] * t, e.x0[2] + e.u[2] * t)
                for ck in self._coarse_keys_around(p, self.coarse_size * 0.75):
                    cell_edges.setdefault(ck, set()).add(e.id)
                if t >= e.t1:
                    break
                t = min(t + step, e.t1)
        self.cell_edges = {k: sorted(v) for k, v in cell_edges.items()}
        self.timings['edges'] = time.perf_counter() - t0

        # 7. corners
        t0 = time.perf_counter()
        corners: List[CornerFeature] = []
        seen_triples: set = set()
        cell_corners: Dict[tuple, set] = {}
        for e in edges:
            a, b = e.a, e.b
            third_ids = adj.get(a, set()) & adj.get(b, set())
            for cid in third_ids:
                triple = tuple(sorted((a, b, cid)))
                if triple in seen_triples:
                    continue
                pa, pb, pc = patches[a], patches[b], patches[cid]
                res = _intersect_three(pa, pb, pc)
                if res is None:
                    continue
                x, det = res
                # the corner must lie on this edge's run and where all three patches are present
                t = _dot(_sub(x, e.x0), e.u)
                if t < e.t0 - cell or t > e.t1 + cell:
                    continue
                if not self._all_present(x, (a, b, cid), fine_patches, span, span2, 2):
                    continue
                seen_triples.add(triple)
                cf = CornerFeature(len(corners), triple, x, det, max(pa.rms, pb.rms, pc.rms))
                corners.append(cf)
                for ck in self._coarse_keys_around(x, self.coarse_size * 0.75):
                    cell_corners.setdefault(ck, set()).add(cf.id)
        self.corners = corners
        self.cell_corners = {k: sorted(v) for k, v in cell_corners.items()}
        self.timings['corners'] = time.perf_counter() - t0
        self.build_seconds = time.perf_counter() - t_start

    # --------------------------------------------------------------- helpers
    def _width_across(self, p: Patch, u: Vec3) -> float:
        """Approximate extent of a patch along the in-plane direction perpendicular to ``u``.

        Uses the covariance of the patch's cell centres (computed once per patch):
        for a uniform strip the extent is sqrt(12 * variance) along that direction.
        """
        w = _cross(u, p.normal)
        cov = self._cell_cov.get(p.id)
        if cov is None:
            cov = self._cell_covariance(p)
            self._cell_cov[p.id] = cov
        var = (cov[0] * w[0] * w[0] + cov[1] * w[1] * w[1] + cov[2] * w[2] * w[2]
               + 2.0 * (cov[3] * w[0] * w[1] + cov[4] * w[0] * w[2] + cov[5] * w[1] * w[2]))
        return math.sqrt(max(12.0 * var, 0.0)) + self.index.cell_size

    def _cell_covariance(self, p: Patch) -> tuple:
        n = len(p.cells)
        if n == 0:
            return (0.0,) * 6
        sx = sy = sz = 0.0
        pts = [self._fine_center(k) for k in p.cells]
        for x, y, z in pts:
            sx += x; sy += y; sz += z
        mx, my, mz = sx / n, sy / n, sz / n
        xx = yy = zz = xy = xz = yz = 0.0
        for x, y, z in pts:
            dx, dy, dz = x - mx, y - my, z - mz
            xx += dx * dx; yy += dy * dy; zz += dz * dz; xy += dx * dy; xz += dx * dz; yz += dy * dz
        return (xx / n, yy / n, zz / n, xy / n, xz / n, yz / n)

    def _fine_center(self, key: int) -> Vec3:
        idx = self.index
        span = idx._span
        ix = key % span
        iy = (key // span) % span
        iz = key // (span * span)
        ox, oy, oz = idx._origin_cell
        s = idx.cell_size
        return ((ix + ox + 0.5) * s, (iy + oy + 0.5) * s, (iz + oz + 0.5) * s)

    def _coarse_key_of_fine(self, key: int) -> tuple:
        # Coarse cells are addressed by absolute indices floor(p / coarse_size); for a fine cell
        # with absolute index i that is i // k (floor division holds for negative indices too).
        idx = self.index
        span = idx._span
        ix = key % span
        iy = (key // span) % span
        iz = key // (span * span)
        ox, oy, oz = idx._origin_cell
        k = self.coarse
        return ((ix + ox) // k, (iy + oy) // k, (iz + oz) // k)

    def _coarse_key(self, p: Vec3) -> tuple:
        s = self.coarse_size
        return (math.floor(p[0] / s), math.floor(p[1] / s), math.floor(p[2] / s))

    def _coarse_keys_around(self, p: Vec3, radius: float) -> List[tuple]:
        s = self.coarse_size
        floor = math.floor
        ix0, ix1 = floor((p[0] - radius) / s), floor((p[0] + radius) / s)
        iy0, iy1 = floor((p[1] - radius) / s), floor((p[1] + radius) / s)
        iz0, iz1 = floor((p[2] - radius) / s), floor((p[2] + radius) / s)
        return [(ix, iy, iz) for ix in range(ix0, ix1 + 1) for iy in range(iy0, iy1 + 1) for iz in range(iz0, iz1 + 1)]

    def _all_present(self, x: Vec3, ids: Sequence[int], fine_patches: Dict[int, List[int]], span: int, span2: int,
                     reach_cells: int) -> bool:
        idx = self.index
        inv = 1.0 / idx.cell_size
        ox, oy, oz = idx._origin_cell
        cx = math.floor(x[0] * inv) - ox
        cy = math.floor(x[1] * inv) - oy
        cz = math.floor(x[2] * inv) - oz
        found = set()
        r = reach_cells
        for dz in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    jx, jy, jz = cx + dx, cy + dy, cz + dz
                    if jx < 0 or jy < 0 or jz < 0 or jx >= span or jy >= span or jz >= span:
                        continue
                    lst = fine_patches.get(jx + jy * span + jz * span2)
                    if lst:
                        for pid in lst:
                            if pid in ids:
                                found.add(pid)
                        if len(found) == len(ids):
                            return True
        return False

    # ----------------------------------------------------------------- query
    def query(self, hit: Vec3, reach: float, tri: Optional[int] = None) -> List[dict]:
        """Features within ``reach`` of ``hit`` (mm, mesh frame), as plain dicts.

        Each dict has: kind ('corner' | 'edge' | 'surface'), key (stable id),
        point, distance, direction (edges), rms, conditioning, patches.
        """
        out: List[dict] = []
        keys = self._coarse_keys_around(hit, reach)
        seen_c: set = set()
        seen_e: set = set()
        seen_p: set = set()
        for ck in keys:
            for cid in self.cell_corners.get(ck, ()):
                if cid in seen_c:
                    continue
                seen_c.add(cid)
                cf = self.corners[cid]
                d = _norm(_sub(cf.point, hit))
                if d <= reach:
                    out.append({'kind': 'corner', 'key': ('c', cid), 'point': cf.point, 'distance': d, 'direction': None,
                                'rms': cf.rms, 'conditioning': cf.det, 'patches': cf.patches})
            for eid in self.cell_edges.get(ck, ()):
                if eid in seen_e:
                    continue
                seen_e.add(eid)
                e = self.edges[eid]
                x, _ = e.closest(hit)
                d = _norm(_sub(x, hit))
                if d <= reach:
                    out.append({'kind': 'edge', 'key': ('e', eid), 'point': x, 'distance': d, 'direction': e.u,
                                'rms': e.rms, 'conditioning': e.sin_angle, 'patches': (e.a, e.b)})
            for pid in self.cell_patches.get(ck, ()):
                if pid in seen_p:
                    continue
                seen_p.add(pid)
                p = self.patches[pid]
                if not p.good and (tri is None or self.tri_patch[tri] != pid):
                    continue
                x = p.project(hit)
                d = _norm(_sub(x, hit))
                if d <= reach and (tri is not None and self.tri_patch[tri] == pid or d <= 0.25 * reach + 0.05):
                    out.append({'kind': 'surface', 'key': ('p', pid), 'point': x, 'distance': d, 'direction': None,
                                'rms': p.rms, 'conditioning': 1.0, 'patches': (pid,)})
        return out

    def stats(self) -> dict:
        good = sum(1 for p in self.patches if p.good)
        return {'patches': len(self.patches), 'good_patches': good, 'edges': len(self.edges), 'corners': len(self.corners),
                'coarse_cells': len(self.cell_patches), 'coarse_size_mm': self.coarse_size,
                'build_seconds': self.build_seconds, 'timings': {k: round(v, 3) for k, v in self.timings.items()}}

    # ------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        """Plain-data snapshot (no class instances) so the cache does not depend on module names."""
        return {
            'patches': [(p.normal, p.point, p.area, p.rms, p.triangles, p.cells, p.good) for p in self.patches],
            'edges': [(e.a, e.b, e.x0, e.u, e.t0, e.t1, e.sin_angle, e.rms) for e in self.edges],
            'corners': [(c.patches, c.point, c.det, c.rms) for c in self.corners],
            'tri_patch': self.tri_patch, 'coarse': self.coarse, 'coarse_size': self.coarse_size,
            'cell_patches': self.cell_patches, 'cell_edges': self.cell_edges, 'cell_corners': self.cell_corners,
            'timings': self.timings, 'build_seconds': self.build_seconds,
        }

    @classmethod
    def from_state(cls, state: dict, index: MeshIndex) -> 'FeatureGraph':
        g = cls(index)
        for pid, (normal, point, area, rms, tris, cells, good) in enumerate(state['patches']):
            p = Patch(pid, tuple(normal), tuple(point), area, tris)
            p.rms, p.cells, p.good = rms, list(cells), good
            g.patches.append(p)
        g.edges = [EdgeFeature(i, a, b, tuple(x0), tuple(u), t0, t1, s, r)
                   for i, (a, b, x0, u, t0, t1, s, r) in enumerate(state['edges'])]
        g.corners = [CornerFeature(i, tuple(pp), tuple(pt), det, rms) for i, (pp, pt, det, rms) in enumerate(state['corners'])]
        g.tri_patch = state['tri_patch']
        g.coarse = state['coarse']
        g.coarse_size = state['coarse_size']
        g.cell_patches = state['cell_patches']
        g.cell_edges = state['cell_edges']
        g.cell_corners = state['cell_corners']
        g.timings = dict(state['timings'])
        g.build_seconds = state['build_seconds']
        return g

    # --------------------------------------------------------------- pickling
    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state['index'] = None
        state['_cell_cov'] = {}
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)


def _line_point(pa: Patch, pb: Patch, u: Vec3) -> Optional[Vec3]:
    """A point on the intersection line of two patches (closest to the origin along u)."""
    det = _dot(pa.normal, _cross(pb.normal, u))
    if abs(det) < 1e-9:
        return None
    c23 = _cross(pb.normal, u)
    c31 = _cross(u, pa.normal)
    return ((c23[0] * pa.offset + c31[0] * pb.offset) / det,
            (c23[1] * pa.offset + c31[1] * pb.offset) / det,
            (c23[2] * pa.offset + c31[2] * pb.offset) / det)


def _intersect_three(p1: Patch, p2: Patch, p3: Patch) -> Optional[Tuple[Vec3, float]]:
    n1, n2, n3 = p1.normal, p2.normal, p3.normal
    det = _dot(n1, _cross(n2, n3))
    if abs(det) < CORNER_MIN_DET:
        return None
    c23 = _cross(n2, n3)
    c31 = _cross(n3, n1)
    c12 = _cross(n1, n2)
    d1, d2, d3 = p1.offset, p2.offset, p3.offset
    return ((c23[0] * d1 + c31[0] * d2 + c12[0] * d3) / det,
            (c23[1] * d1 + c31[1] * d2 + c12[1] * d3) / det,
            (c23[2] * d1 + c31[2] * d2 + c12[2] * d3) / det), abs(det)
