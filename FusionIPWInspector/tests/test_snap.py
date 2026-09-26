"""Deterministic tests for the snapping core (mesh index + feature reconstruction).

    python -m unittest discover -s FusionIPWInspector/tests

All meshes are built analytically so the expected corners, edges and planes are
known exactly.
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from FusionIPWInspector.core.mesh_index import MeshIndex  # noqa: E402
from FusionIPWInspector.core import snap as S  # noqa: E402


# --------------------------------------------------------------- mesh builders
def quad(a, b, c, d):
    """Two triangles for the quad a-b-c-d (counter-clockwise)."""
    return [a, b, c, a, c, d]


def box(lo, hi):
    """Closed axis-aligned box, outward normals, 12 triangles."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    p = lambda x, y, z: (x, y, z)  # noqa: E731
    tris = []
    tris += quad(p(x0, y0, z1), p(x1, y0, z1), p(x1, y1, z1), p(x0, y1, z1))   # top +z
    tris += quad(p(x0, y1, z0), p(x1, y1, z0), p(x1, y0, z0), p(x0, y0, z0))   # bottom -z
    tris += quad(p(x0, y0, z0), p(x1, y0, z0), p(x1, y0, z1), p(x0, y0, z1))   # front -y
    tris += quad(p(x1, y1, z0), p(x0, y1, z0), p(x0, y1, z1), p(x1, y1, z1))   # back +y
    tris += quad(p(x1, y0, z0), p(x1, y1, z0), p(x1, y1, z1), p(x1, y0, z1))   # right +x
    tris += quad(p(x0, y1, z0), p(x0, y0, z0), p(x0, y0, z1), p(x0, y1, z1))   # left -x
    return tris


def subdivide(tris, n):
    """Split every triangle into n*n smaller ones (uniform tessellation)."""
    out = []
    for i in range(0, len(tris), 3):
        a, b, c = tris[i], tris[i + 1], tris[i + 2]
        ab = tuple((b[k] - a[k]) / n for k in range(3))
        ac = tuple((c[k] - a[k]) / n for k in range(3))
        for r in range(n):
            for s in range(n - r):
                p0 = tuple(a[k] + ab[k] * s + ac[k] * r for k in range(3))
                p1 = tuple(a[k] + ab[k] * (s + 1) + ac[k] * r for k in range(3))
                p2 = tuple(a[k] + ab[k] * s + ac[k] * (r + 1) for k in range(3))
                out += [p0, p1, p2]
                if s + r + 1 < n:
                    p3 = tuple(a[k] + ab[k] * (s + 1) + ac[k] * (r + 1) for k in range(3))
                    out += [p1, p3, p2]
    return out


def jitter(tris, amplitude, seed=0):
    rnd = random.Random(seed)
    return [tuple(c + rnd.uniform(-amplitude, amplitude) for c in v) for v in tris]


def flat(tris):
    return [c for v in tris for c in v]


def index_of(tris, cell=None):
    return MeshIndex.from_coords(flat(tris), cell)


def close(a, b, tol=1e-6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


BOX = box((0, 0, 0), (40, 30, 20))


# ----------------------------------------------------------------- index
class MeshIndexTests(unittest.TestCase):
    def test_triangles_near_finds_local_triangles_only(self):
        idx = index_of(subdivide(BOX, 8), cell=2.0)
        near = idx.triangles_near((40.0, 30.0, 20.0), 1.0)
        self.assertGreater(len(near), 0)
        self.assertLess(len(near), idx.triangle_count // 4)
        for t in near:
            self.assertLessEqual(math.sqrt(idx.distance_sq_to_triangle((40.0, 30.0, 20.0), t)), 1.0 + 1e-9)

    def test_distance_to_triangle(self):
        idx = index_of([(0, 0, 0), (10, 0, 0), (0, 10, 0)], cell=5.0)
        self.assertAlmostEqual(idx.distance_sq_to_triangle((2, 2, 3), 0), 9.0)      # above the face
        self.assertAlmostEqual(idx.distance_sq_to_triangle((-3, 0, 0), 0), 9.0)     # beyond vertex a
        self.assertAlmostEqual(idx.distance_sq_to_triangle((5, -4, 0), 0), 16.0)    # beyond edge ab

    def test_degenerate_triangles_have_zero_area(self):
        idx = index_of([(0, 0, 0), (1, 1, 1), (2, 2, 2)], cell=1.0)
        n, a = idx.normal_and_area(0)
        self.assertEqual(a, 0.0)

    def test_duplicate_vertices_do_not_break_queries(self):
        tris = subdivide(BOX, 4)
        idx = index_of(tris + tris, cell=3.0)
        self.assertEqual(idx.triangle_count, 2 * len(tris) // 3)
        res = S.snap(idx, (39.6, 29.6, 19.6), 1.0)
        self.assertEqual(res.best.kind, S.CORNER)
        self.assertTrue(close(res.best.point, (40, 30, 20), 1e-6))


# --------------------------------------------------------------- features
class PlaneFitting(unittest.TestCase):
    def test_single_plane_gives_surface_projection(self):
        idx = index_of(subdivide(BOX, 6), cell=3.0)
        res = S.snap(idx, (20.0, 15.0, 20.3), 1.0)   # slightly above the top face centre
        self.assertEqual(res.best.kind, S.SURFACE)
        self.assertTrue(close(res.best.point, (20.0, 15.0, 20.0)))
        self.assertLess(res.best.residual, 1e-9)

    def test_noisy_plane_residual_is_reported(self):
        tris = jitter(subdivide(BOX, 6), 0.004, seed=3)
        idx = index_of(tris, cell=3.0)
        res = S.snap(idx, (20.0, 15.0, 20.0), 1.5)
        self.assertEqual(res.best.kind, S.SURFACE)
        self.assertLess(abs(res.best.point[2] - 20.0), 0.01)
        self.assertGreater(res.best.residual, 0.0)
        self.assertLess(res.best.residual, 0.01)

    def test_irregular_tessellation_still_one_plane(self):
        coarse = subdivide(BOX, 2)
        fine = subdivide(BOX, 9)
        idx = index_of(coarse + fine, cell=3.0)
        res = S.snap(idx, (10.0, 10.0, 20.0), 1.0)
        self.assertEqual(res.best.kind, S.SURFACE)
        self.assertEqual(len([c for c in res.candidates if c.kind == S.SURFACE]), 1)


class EdgeReconstruction(unittest.TestCase):
    def test_two_plane_edge_from_offset_hit(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        # near the top/front edge y = 0, z = 20, away from the corners
        res = S.snap(idx, (20.0, 0.4, 19.7), 1.0)
        self.assertEqual(res.best.kind, S.EDGE)
        self.assertTrue(close(res.best.point, (20.0, 0.0, 20.0), 1e-6))
        self.assertTrue(abs(abs(res.best.direction[0]) - 1.0) < 1e-9)   # along x
        self.assertAlmostEqual(res.best.conditioning, 1.0)

    def test_internal_triangulation_edges_are_not_edges(self):
        # The diagonal of the top face quad runs from (0,0,20) to (40,30,20); hovering on it must give SURFACE.
        idx = index_of(BOX, cell=10.0)
        res = S.snap(idx, (20.0, 15.0, 20.0), 3.0)
        self.assertEqual(res.best.kind, S.SURFACE)
        self.assertFalse(any(c.kind == S.EDGE for c in res.candidates))

    def test_near_parallel_planes_do_not_make_an_edge(self):
        # two coplanar-ish sheets meeting at 5 degrees: not a machining edge
        a = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 0, 0), (20, 20, 0), (0, 20, 0)]
        ang = math.radians(5.0)
        b = [(20, 0, 0), (40, 0, 20 * math.tan(ang)), (40, 20, 20 * math.tan(ang)), (20, 0, 0), (40, 20, 20 * math.tan(ang)), (20, 20, 0)]
        idx = index_of(subdivide(a + b, 4), cell=4.0)
        res = S.snap(idx, (20.0, 10.0, 0.0), 2.0)
        self.assertNotEqual(res.best.kind, S.EDGE)
        self.assertNotEqual(res.best.kind, S.CORNER)

    def test_edge_requires_support_from_both_planes(self):
        # A plane and a far away second plane whose intersection line passes near the hit but is unsupported.
        a = subdivide([(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 0, 0), (20, 20, 0), (0, 20, 0)], 4)   # z = 0
        b = subdivide([(0, 0, 30), (0, 20, 30), (0, 20, 50), (0, 0, 30), (0, 20, 50), (0, 0, 50)], 4)  # x = 0, z 30..50
        idx = index_of(a + b, cell=4.0)
        res = S.snap(idx, (0.3, 10.0, 0.0), 1.0)
        self.assertNotEqual(res.best.kind, S.EDGE)


class CornerReconstruction(unittest.TestCase):
    def test_three_plane_corner_from_offset_hit(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        res = S.snap(idx, (39.4, 29.5, 19.6), 1.0)
        self.assertEqual(res.best.kind, S.CORNER)
        self.assertTrue(close(res.best.point, (40.0, 30.0, 20.0), 1e-6))
        self.assertAlmostEqual(res.best.conditioning, 1.0)
        self.assertLess(res.best.distance_from_hit, 1.0)

    def test_corner_of_a_tab_on_heavily_tessellated_mesh(self):
        tab = subdivide(box((10, 10, 20), (16, 13, 21.3)), 6)
        idx = index_of(subdivide(BOX, 6) + tab, cell=2.0)
        res = S.snap(idx, (15.7, 12.6, 21.1), 0.8)
        self.assertEqual(res.best.kind, S.CORNER)
        self.assertTrue(close(res.best.point, (16.0, 13.0, 21.3), 1e-6))

    def test_unstable_three_plane_intersection_is_rejected(self):
        # three planes sharing a line (a "book"): det = 0, must not produce a corner
        base = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 0, 0), (20, 20, 0), (0, 20, 0)]
        wall = [(0, 0, 0), (0, 20, 0), (0, 20, 20), (0, 0, 0), (0, 20, 20), (0, 0, 20)]
        slope = [(0, 0, 0), (0, 20, 0), (-10, 20, 10), (0, 0, 0), (-10, 20, 10), (-10, 0, 10)]
        idx = index_of(subdivide(base + wall + slope, 4), cell=4.0)
        res = S.snap(idx, (0.2, 10.0, 0.2), 1.5)
        self.assertNotEqual(res.best.kind, S.CORNER)
        self.assertEqual(res.best.kind, S.EDGE)

    def test_corner_far_from_hit_is_rejected(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        res = S.snap(idx, (35.0, 15.0, 20.0), 1.0)   # 5 mm from the nearest corner
        self.assertEqual(res.best.kind, S.SURFACE)


class CandidateOrderingAndHysteresis(unittest.TestCase):
    def test_priority_order_corner_edge_surface(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        res = S.snap(idx, (39.5, 29.5, 19.5), 1.0)
        kinds = [c.kind for c in res.candidates]
        self.assertEqual(kinds[0], S.CORNER)
        self.assertEqual(kinds[-1], S.RAW)
        self.assertEqual(kinds, sorted(kinds, key=lambda k: -S.PRIORITY[k]))

    def test_tracker_keeps_corner_while_cursor_wobbles(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        tracker = S.SnapTracker()
        first = tracker.update(S.snap(idx, (39.4, 29.5, 19.6), 1.0))
        self.assertEqual(first.kind, S.CORNER)
        # wobble a little: corner must stay selected
        for hit in ((39.2, 29.6, 19.7), (39.5, 29.1, 19.4), (39.0, 29.3, 19.9)):
            cur = tracker.update(S.snap(idx, hit, 1.0))
            self.assertEqual(cur.kind, S.CORNER)
        # move clearly away along the top edge: corner disappears from candidates, edge takes over
        cur = tracker.update(S.snap(idx, (30.0, 29.6, 19.7), 1.0))
        self.assertEqual(cur.kind, S.EDGE)

    def test_tab_cycles_through_candidates(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        tracker = S.SnapTracker()
        tracker.update(S.snap(idx, (39.4, 29.5, 19.6), 1.0))
        seen = [tracker.current.kind]
        for _ in range(len(tracker.result.candidates) - 1):
            seen.append(tracker.cycle().kind)
        self.assertIn(S.EDGE, seen)
        self.assertIn(S.SURFACE, seen)
        self.assertIn(S.RAW, seen)

    def test_cycled_choice_survives_hover_jitter(self):
        # Fusion re-fires a hover right before the click: the user's cycled choice must win
        # over the automatic ranking until the cursor leaves that feature.
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        tracker = S.SnapTracker()
        tracker.update(S.snap(idx, (39.4, 29.5, 19.6), 1.0))
        self.assertEqual(tracker.current.kind, S.CORNER)
        chosen = tracker.cycle()
        while chosen.kind == S.CORNER:
            chosen = tracker.cycle()
        again = tracker.update(S.snap(idx, (39.4, 29.5, 19.6), 1.0))
        self.assertEqual(again.kind, chosen.kind)
        wobble = tracker.update(S.snap(idx, (39.3, 29.6, 19.7), 1.0))
        self.assertEqual(wobble.kind, chosen.kind)
        # far away: automatic ranking again
        tracker.update(S.snap(idx, (20.0, 15.0, 20.0), 1.0))
        self.assertFalse(tracker.manual)


class ScreenSpaceTolerance(unittest.TestCase):
    def test_radius_scales_with_view(self):
        # Same corner, same pixel tolerance: zoomed out (large mm per pixel) still finds the corner,
        # zoomed in (small mm per pixel) with the cursor 0.3 mm away still finds it.
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        far_radius = 12 * 0.25    # 12 px at 0.25 mm/px
        near_radius = 12 * 0.03   # 12 px at 0.03 mm/px
        self.assertEqual(S.snap(idx, (38.0, 28.5, 20.0), far_radius).best.kind, S.CORNER)
        self.assertEqual(S.snap(idx, (39.8, 29.75, 19.8), near_radius).best.kind, S.CORNER)
        self.assertEqual(S.snap(idx, (38.0, 28.5, 20.0), near_radius).best.kind, S.SURFACE)


if __name__ == '__main__':
    unittest.main()
