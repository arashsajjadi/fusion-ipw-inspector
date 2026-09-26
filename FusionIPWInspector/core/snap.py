"""Feature reconstruction around a point on the stock mesh (pure Python).

Given a hit point on the mesh and a search radius (derived from a screen-space
tolerance by the caller), the triangles nearby are grouped into locally planar
regions. From those planes the likely machining feature is reconstructed:

    three well-conditioned planes  -> CORNER (their intersection point)
    two distinct planes            -> EDGE   (hit projected onto their intersection line)
    one plane                      -> SURFACE (hit projected onto the plane)
    otherwise                      -> RAW    (the hit itself)

Internal triangulation edges never produce an EDGE: only boundaries between two
fitted planes do. Every reconstructed feature must be supported by triangles
of each contributing plane close to the feature, must lie near the hit, and
must be well conditioned; otherwise the next simpler feature is used.

``SnapTracker`` adds hysteresis so the preview does not flicker between
candidates while the cursor moves a few pixels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .mesh_index import MeshIndex, Vec3, _cross, _dot, _norm, _sub

CORNER, EDGE, SURFACE, RAW = 'corner', 'edge', 'surface', 'raw'
PRIORITY = {CORNER: 3, EDGE: 2, SURFACE: 1, RAW: 0}

# Two normals within this angle belong to the same plane; also requires the
# triangle to lie on the plane within PLANE_OFFSET_TOL.
CLUSTER_ANGLE_DEG = 12.0
PLANE_OFFSET_TOL = 0.05          # mm
MIN_PLANE_AREA_FRACTION = 0.03   # of the neighbourhood area
MIN_PLANE_TRIANGLES = 1
EDGE_MIN_ANGLE_DEG = 20.0        # planes closer than this are treated as one
CORNER_MIN_DET = 0.15            # |det| of the three unit normals (1 = orthogonal)
SUPPORT_RADIUS_FACTOR = 1.5      # a plane supports a feature if it has a triangle this close (x radius)
REACH_FACTOR = 1.5               # a feature may be this far (x radius) from the hit
MAX_RESIDUAL = 0.05              # mm RMS of the plane fits used for a feature


@dataclass
class Plane:
    normal: Vec3
    point: Vec3
    area: float
    triangles: List[int]
    rms: float = 0.0

    @property
    def offset(self) -> float:
        return _dot(self.normal, self.point)

    def distance(self, p: Vec3) -> float:
        return _dot(self.normal, p) - self.offset

    def project(self, p: Vec3) -> Vec3:
        d = self.distance(p)
        return (p[0] - self.normal[0] * d, p[1] - self.normal[1] * d, p[2] - self.normal[2] * d)


@dataclass
class Candidate:
    kind: str
    point: Vec3
    planes: List[Plane] = field(default_factory=list)
    residual: float = 0.0           # worst RMS plane residual (mm)
    conditioning: float = 1.0       # |det| for corners, sin(angle) for edges, 1 for surfaces
    distance_from_hit: float = 0.0  # mm
    direction: Optional[Vec3] = None  # edge direction (unit), for drawing
    label: str = ''
    key: Optional[tuple] = None     # stable identity across hovers (feature id, or a geometric key)

    @property
    def priority(self) -> int:
        return PRIORITY[self.kind]

    def confidence(self) -> str:
        """Physically meaningful summary: based on residual and conditioning only."""
        if self.kind == RAW:
            return 'raw surface hit'
        if self.residual <= 0.01 and self.conditioning >= 0.5:
            return 'high'
        if self.residual <= MAX_RESIDUAL and self.conditioning >= 0.25:
            return 'medium'
        return 'low'

    def method(self) -> str:
        return {CORNER: '3-plane intersection', EDGE: '2-plane intersection',
                SURFACE: 'plane fit', RAW: 'mesh hit'}[self.kind]


@dataclass
class SnapResult:
    candidates: List[Candidate]
    hit: Vec3
    radius: float
    triangle_count: int
    plane_count: int

    @property
    def best(self) -> Candidate:
        return self.candidates[0]


# ---------------------------------------------------------------- planes
def fit_planes(index: MeshIndex, triangles: Sequence[int]) -> List[Plane]:
    """Group triangles into planes by normal direction and offset, largest area first."""
    items = []
    for t in triangles:
        n, a = index.normal_and_area(t)
        if a > 1e-9:
            items.append((a, n, t))
    if not items:
        return []
    items.sort(key=lambda it: -it[0])
    cos_tol = math.cos(math.radians(CLUSTER_ANGLE_DEG))
    planes: List[Plane] = []
    for area, n, t in items:
        c = index.centroid(t)
        placed = False
        for pl in planes:
            if _dot(n, pl.normal) >= cos_tol and abs(pl.distance(c)) <= PLANE_OFFSET_TOL:
                # area-weighted running update of normal and point
                w = pl.area + area
                nn = ((pl.normal[0] * pl.area + n[0] * area) / w, (pl.normal[1] * pl.area + n[1] * area) / w,
                      (pl.normal[2] * pl.area + n[2] * area) / w)
                ln = _norm(nn) or 1.0
                pl.normal = (nn[0] / ln, nn[1] / ln, nn[2] / ln)
                pl.point = ((pl.point[0] * pl.area + c[0] * area) / w, (pl.point[1] * pl.area + c[1] * area) / w,
                            (pl.point[2] * pl.area + c[2] * area) / w)
                pl.area = w
                pl.triangles.append(t)
                placed = True
                break
        if not placed:
            planes.append(Plane(normal=n, point=c, area=area, triangles=[t]))
    total = sum(pl.area for pl in planes)
    kept = [pl for pl in planes if pl.area >= MIN_PLANE_AREA_FRACTION * total and len(pl.triangles) >= MIN_PLANE_TRIANGLES]
    for pl in kept:
        pl.rms = _plane_rms(index, pl)
    kept.sort(key=lambda pl: -pl.area)
    return kept


def _plane_rms(index: MeshIndex, pl: Plane) -> float:
    acc, cnt = 0.0, 0
    for t in pl.triangles:
        for v in index.vertices(t):
            d = pl.distance(v)
            acc += d * d
            cnt += 1
    return math.sqrt(acc / cnt) if cnt else 0.0


def _supports(index: MeshIndex, pl: Plane, p: Vec3, radius: float) -> bool:
    r2 = radius * radius
    for t in pl.triangles:
        if index.distance_sq_to_triangle(p, t) <= r2:
            return True
    return False


# ------------------------------------------------------------ intersections
def intersect_three(p1: Plane, p2: Plane, p3: Plane) -> Optional[Tuple[Vec3, float]]:
    n1, n2, n3 = p1.normal, p2.normal, p3.normal
    det = _dot(n1, _cross(n2, n3))
    if abs(det) < CORNER_MIN_DET:
        return None
    d1, d2, d3 = p1.offset, p2.offset, p3.offset
    c23 = _cross(n2, n3)
    c31 = _cross(n3, n1)
    c12 = _cross(n1, n2)
    x = ((c23[0] * d1 + c31[0] * d2 + c12[0] * d3) / det,
         (c23[1] * d1 + c31[1] * d2 + c12[1] * d3) / det,
         (c23[2] * d1 + c31[2] * d2 + c12[2] * d3) / det)
    return x, abs(det)


def intersect_two(p1: Plane, p2: Plane) -> Optional[Tuple[Vec3, Vec3, float]]:
    """Line (point, unit direction) of two planes and sin of their angle."""
    d = _cross(p1.normal, p2.normal)
    s = _norm(d)
    if s < math.sin(math.radians(EDGE_MIN_ANGLE_DEG)):
        return None
    u = (d[0] / s, d[1] / s, d[2] / s)
    # Point on the line: solve with the third plane through the origin with normal u.
    n3 = Plane(normal=u, point=(0.0, 0.0, 0.0), area=0.0, triangles=[])
    n3.point = (0.0, 0.0, 0.0)
    det = _dot(p1.normal, _cross(p2.normal, u))
    c23 = _cross(p2.normal, u)
    c31 = _cross(u, p1.normal)
    x = ((c23[0] * p1.offset + c31[0] * p2.offset) / det,
         (c23[1] * p1.offset + c31[1] * p2.offset) / det,
         (c23[2] * p1.offset + c31[2] * p2.offset) / det)
    return x, u, s


def _project_on_line(p: Vec3, x: Vec3, u: Vec3) -> Vec3:
    t = _dot(_sub(p, x), u)
    return (x[0] + u[0] * t, x[1] + u[1] * t, x[2] + u[2] * t)


# ------------------------------------------------------------------ snap
def snap(index: MeshIndex, hit: Vec3, radius: float) -> SnapResult:
    """All valid candidates around ``hit`` (mm, mesh frame), best first."""
    tris = index.triangles_near(hit, radius)
    planes = fit_planes(index, tris)
    reach = radius * REACH_FACTOR
    support_r = radius * SUPPORT_RADIUS_FACTOR
    cands: List[Candidate] = []

    good = [pl for pl in planes if pl.rms <= MAX_RESIDUAL]
    # corners: every triple of the largest (up to 5) planes
    top = good[:5]
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            for k in range(j + 1, len(top)):
                res = intersect_three(top[i], top[j], top[k])
                if res is None:
                    continue
                x, det = res
                dist = _norm(_sub(x, hit))
                if dist > reach:
                    continue
                if not all(_supports(index, pl, x, support_r) for pl in (top[i], top[j], top[k])):
                    continue
                cands.append(Candidate(CORNER, x, [top[i], top[j], top[k]], max(pl.rms for pl in (top[i], top[j], top[k])),
                                       det, dist, label='CORNER'))
    # edges: every pair
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            res = intersect_two(top[i], top[j])
            if res is None:
                continue
            x0, u, s = res
            x = _project_on_line(hit, x0, u)
            dist = _norm(_sub(x, hit))
            if dist > reach:
                continue
            if not (_supports(index, top[i], x, support_r) and _supports(index, top[j], x, support_r)):
                continue
            cands.append(Candidate(EDGE, x, [top[i], top[j]], max(top[i].rms, top[j].rms), s, dist, direction=u, label='EDGE'))
    # surfaces: planes that contain (or nearly contain) the hit
    for pl in good:
        x = pl.project(hit)
        dist = _norm(_sub(x, hit))
        if dist <= reach and _supports(index, pl, x, support_r):
            cands.append(Candidate(SURFACE, x, [pl], pl.rms, 1.0, dist, label='SURFACE'))
    cands.append(Candidate(RAW, hit, [], 0.0, 1.0, 0.0, label='POINT'))
    cands.sort(key=lambda c: (-c.priority, c.distance_from_hit))
    # keep the best of each kind first, then the rest (for cycling)
    return SnapResult(cands, hit, radius, len(tris), len(planes))


# ------------------------------------------------------------- hysteresis
class SnapTracker:
    """Keeps the previewed candidate stable while the cursor moves a little.

    A new best candidate replaces the current one only when the current kind is
    no longer available near the cursor, when the new one is of a higher kind
    and clearly closer, or when the cursor left the current feature by more than
    ``switch_factor`` times the search radius. Manual cycling (Tab) is honoured
    until the cursor moves away from the cycled feature.
    """

    def __init__(self, switch_factor: float = 0.6) -> None:
        self.current: Optional[Candidate] = None
        self.result: Optional[SnapResult] = None
        self.cycle_index = 0
        self.switch_factor = switch_factor
        self.manual = False        # True after cycling: the user's choice beats the ranking rules

    def update(self, result: SnapResult) -> Candidate:
        self.result = result
        cands = result.candidates
        best = cands[0]
        cur = self.current
        if cur is None:
            self.current, self.cycle_index = best, 0
            return best
        # Is a candidate equivalent to the current one still available?
        same = _find_equivalent(cands, cur, result.radius)
        if same is None:
            self.current, self.cycle_index, self.manual = best, 0, False
            return best
        if self.manual:
            self.current = same
            self.cycle_index = cands.index(same)
            return same
        if best.priority > same.priority and best.distance_from_hit < same.distance_from_hit + self.switch_factor * result.radius:
            self.current, self.cycle_index = best, 0
            return best
        if best.priority == same.priority and best.distance_from_hit + self.switch_factor * result.radius < same.distance_from_hit:
            self.current, self.cycle_index = best, 0
            return best
        self.current = same
        self.cycle_index = cands.index(same)
        return same

    def cycle(self) -> Optional[Candidate]:
        if self.result is None or not self.result.candidates:
            return None
        cands = self.result.candidates
        self.cycle_index = (self.cycle_index + 1) % len(cands)
        self.current = cands[self.cycle_index]
        self.manual = len(cands) > 1
        return self.current

    def reset(self) -> None:
        self.current, self.result, self.cycle_index, self.manual = None, None, 0, False


def _find_equivalent(cands: Sequence[Candidate], cur: Candidate, radius: float) -> Optional[Candidate]:
    tol = 0.5 * radius + 1e-6
    for c in cands:
        if c.kind != cur.kind:
            continue
        if c.kind in (CORNER, RAW) and _norm(_sub(c.point, cur.point)) <= tol:
            return c
        if c.kind == EDGE and cur.direction is not None and c.direction is not None:
            if abs(_dot(c.direction, cur.direction)) > 0.99 and _norm(_cross(_sub(c.point, cur.point), c.direction)) <= tol:
                return c
        if c.kind == SURFACE and cur.planes and c.planes:
            a, b = c.planes[0], cur.planes[0]
            if _dot(a.normal, b.normal) > 0.99 and abs(a.distance(b.point)) <= PLANE_OFFSET_TOL * 2:
                return c
    return None


def keyed(result: SnapResult, quantum: float = 0.05) -> List[Candidate]:
    """Give locally reconstructed candidates geometric identities (rounded to ``quantum`` mm).

    A corner is identified by its position, an edge by its line, a surface by
    its plane, so the same feature found from two neighbouring hits compares
    equal even though it was rebuilt from a different set of triangles.
    """
    q = 1.0 / quantum
    for c in result.candidates:
        if c.key is not None:
            continue
        if c.kind == CORNER:
            c.key = ('lc', round(c.point[0] * q), round(c.point[1] * q), round(c.point[2] * q))
        elif c.kind == EDGE and c.direction is not None:
            u = c.direction
            if (u[0], u[1], u[2]) < (0.0, 0.0, 0.0):
                u = (-u[0], -u[1], -u[2])
            foot = _sub(c.point, tuple(v * _dot(c.point, u) for v in u))   # closest point of the line to the origin
            c.key = ('le', round(u[0] * 50), round(u[1] * 50), round(u[2] * 50),
                     round(foot[0] * q), round(foot[1] * q), round(foot[2] * q))
        elif c.kind == SURFACE and c.planes:
            n = c.planes[0].normal
            c.key = ('ls', round(n[0] * 50), round(n[1] * 50), round(n[2] * 50), round(c.planes[0].offset * q))
        else:
            c.key = ('raw',)
    return result.candidates


class MagneticTracker:
    """CAD-style snapping: acquire a feature within the tolerance, hold it until the cursor
    clearly leaves it, and never alternate between candidates on tiny cursor motion.

    * A feature is acquired when it is within ``radius`` (the screen tolerance).
    * Once acquired it is retained while the same feature (by key) is still
      within ``retain_factor`` x radius, even if a different feature of the
      same kind is now nearer.
    * A higher-priority feature within the acquire radius takes over (moving
      along an edge into a corner snaps to the corner); a same-priority feature
      only takes over when it is within the acquire radius and clearly closer.
    * A manually cycled choice is honoured while the feature stays within reach.
    """

    def __init__(self, retain_factor: float = 2.0, switch_factor: float = 0.5) -> None:
        self.retain_factor = retain_factor
        self.switch_factor = switch_factor
        self.candidates: List[Candidate] = []
        self.current: Optional[Candidate] = None
        self.cycle_index = 0
        self.manual = False
        self.radius = 1.0

    def update(self, candidates: List[Candidate], radius: float) -> Candidate:
        """``candidates`` sorted best first (priority, then distance); returns the choice."""
        self.candidates = candidates
        self.radius = radius
        retain = radius * self.retain_factor
        cur = self.current
        choice: Optional[Candidate] = None
        if cur is not None:
            same = next((c for c in candidates if c.key == cur.key and c.distance_from_hit <= retain), None)
            if same is None:
                self.manual = False
            elif self.manual:
                choice = same
            else:
                better = next((c for c in candidates if c.priority > same.priority and c.distance_from_hit <= radius), None)
                if better is not None:
                    choice = better
                else:
                    alt = next((c for c in candidates if c.priority == same.priority and c.key != same.key
                                and c.distance_from_hit <= radius
                                and c.distance_from_hit + self.switch_factor * radius < same.distance_from_hit), None)
                    choice = alt if alt is not None else same
        if choice is None:
            choice = next((c for c in candidates if c.distance_from_hit <= radius), None)
            if choice is None:
                choice = next((c for c in candidates if c.priority <= PRIORITY[SURFACE]), candidates[-1])
        self.current = choice
        self.cycle_index = candidates.index(choice) if choice in candidates else 0
        return choice

    def cycle(self) -> Optional[Candidate]:
        if not self.candidates:
            return None
        self.cycle_index = (self.cycle_index + 1) % len(self.candidates)
        self.current = self.candidates[self.cycle_index]
        self.manual = len(self.candidates) > 1
        return self.current

    def reset(self) -> None:
        self.candidates, self.current, self.cycle_index, self.manual = [], None, 0, False


def describe(c: Candidate) -> Dict[str, object]:
    """Diagnostics for the Advanced panel."""
    return {
        'kind': c.kind,
        'method': c.method(),
        'point_mm': tuple(round(v, 4) for v in c.point),
        'residual_rms_mm': round(c.residual, 4),
        'conditioning': round(c.conditioning, 3),
        'distance_from_hit_mm': round(c.distance_from_hit, 3),
        'planes': [{'normal': tuple(round(v, 4) for v in pl.normal), 'area_mm2': round(pl.area, 3),
                    'triangles': len(pl.triangles), 'rms_mm': round(pl.rms, 4)} for pl in c.planes],
    }
