"""Deterministic tests for the precomputed feature graph and the ray cast.

    python -m unittest discover -s FusionIPWInspector/tests

Geometry is analytic (a tessellated box with a tab-like second box), so every
expected corner, edge line and plane is known exactly.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from FusionIPWInspector.core import features as F  # noqa: E402
from FusionIPWInspector.core.mesh_index import MeshIndex  # noqa: E402
from test_snap import BOX, box, close, flat, index_of, jitter, subdivide  # noqa: E402


def graph_of(tris, cell=None):
    idx = MeshIndex.from_coords(flat(tris), cell)
    return idx, F.FeatureGraph.build(idx)


def kinds(cands):
    return [c['kind'] for c in cands]


def best(cands, kind):
    items = [c for c in cands if c['kind'] == kind]
    return min(items, key=lambda c: c['distance']) if items else None


class PatchExtraction(unittest.TestCase):
    def test_box_faces_become_six_patches(self):
        idx, g = graph_of(subdivide(BOX, 10), cell=3.0)
        good = [p for p in g.patches if p.good and p.area > 50]
        self.assertEqual(len(good), 6)
        normals = sorted(tuple(round(v) for v in p.normal) for p in good)
        self.assertEqual(normals, sorted([(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]))
        for p in good:
            self.assertLess(p.rms, 1e-6)

    def test_internal_triangulation_does_not_split_a_face(self):
        idx, g = graph_of(subdivide(BOX, 12), cell=2.5)
        top = [p for p in g.patches if p.good and round(p.normal[2]) == 1]
        self.assertEqual(len(top), 1)
        self.assertAlmostEqual(top[0].area, 40 * 30, delta=1e-3)

    def test_noisy_face_keeps_one_patch_with_reported_residual(self):
        idx, g = graph_of(jitter(subdivide(BOX, 10), 0.01), cell=3.0)
        top = [p for p in g.patches if p.good and round(p.normal[2]) == 1 and p.area > 50]
        self.assertEqual(len(top), 1)
        self.assertGreater(top[0].rms, 0.0)
        self.assertLess(top[0].rms, 0.02)


class EdgesAndCorners(unittest.TestCase):
    def test_all_twelve_edges_and_eight_corners(self):
        idx, g = graph_of(subdivide(BOX, 10), cell=3.0)
        self.assertEqual(len(g.corners), 8)
        expected = sorted((x, y, z) for x in (0, 40) for y in (0, 30) for z in (0, 20))
        got = sorted(tuple(round(v, 6) for v in c.point) for c in g.corners)
        self.assertEqual(got, expected)
        self.assertEqual(len(g.edges), 12)
        for e in g.edges:
            axis = [abs(round(v)) for v in e.u]
            self.assertEqual(sum(axis), 1)

    def test_query_near_corner_returns_corner_first(self):
        idx, g = graph_of(subdivide(BOX, 10), cell=3.0)
        hit = (39.4, 29.5, 20.0)          # on the top face, 0.8 mm from the corner
        cands = g.query(hit, 1.5)
        c = best(cands, 'corner')
        self.assertIsNotNone(c)
        self.assertTrue(close(c['point'], (40, 30, 20), 1e-6))
        self.assertAlmostEqual(c['distance'], math.sqrt(0.36 + 0.25), places=6)
        self.assertIsNotNone(best(cands, 'edge'))
        self.assertIsNotNone(best(cands, 'surface'))

    def test_query_on_edge_projects_onto_line(self):
        idx, g = graph_of(subdivide(BOX, 10), cell=3.0)
        hit = (20.0, 29.6, 20.0)
        e = best(g.query(hit, 1.0), 'edge')
        self.assertIsNotNone(e)
        self.assertTrue(close(e['point'], (20.0, 30.0, 20.0), 1e-6))
        self.assertAlmostEqual(abs(e['direction'][0]), 1.0, places=6)

    def test_query_far_from_features_is_surface_only(self):
        idx, g = graph_of(subdivide(BOX, 10), cell=3.0)
        cands = g.query((20.0, 15.0, 20.0), 1.0)
        self.assertEqual(kinds(cands), ['surface'])
        self.assertTrue(close(cands[0]['point'], (20.0, 15.0, 20.0), 1e-6))

    def test_two_corners_of_one_edge_share_the_fitted_planes(self):
        # tessellation noise must not make the shared coordinates drift between corners
        idx, g = graph_of(jitter(subdivide(BOX, 10), 0.01, seed=3), cell=3.0)
        c1 = best(g.query((39.5, 29.5, 20.0), 1.5), 'corner')
        c2 = best(g.query((0.5, 29.5, 20.0), 1.5), 'corner')
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        # both corners come from the same fitted top and back patches and lie exactly on them
        shared = set(c1['patches']) & set(c2['patches'])
        self.assertEqual(len(shared), 2)
        for pid in shared:
            patch = g.patches[pid]
            self.assertAlmostEqual(patch.distance(c1['point']), 0.0, places=9)
            self.assertAlmostEqual(patch.distance(c2['point']), 0.0, places=9)

    def test_two_corners_of_one_edge_share_coordinates_exactly_on_clean_planes(self):
        idx, g = graph_of(subdivide(BOX, 10), cell=3.0)
        c1 = best(g.query((39.5, 29.5, 20.0), 1.5), 'corner')
        c2 = best(g.query((0.5, 29.5, 20.0), 1.5), 'corner')
        self.assertEqual(c1['point'][1], c2['point'][1])
        self.assertEqual(c1['point'][2], c2['point'][2])
        self.assertEqual((c1['point'][0], c2['point'][0]), (40.0, 0.0))

    def test_tab_on_a_plate_has_its_own_corners(self):
        plate = subdivide(box((0, 0, 0), (40, 30, 5)), 10)
        tab = subdivide(box((10, 10, 5), (14, 13, 8)), 4)      # a 4 x 3 x 3 mm tab on top
        idx, g = graph_of(plate + tab, cell=1.0)
        pts = sorted(tuple(round(v, 6) for v in c.point) for c in g.corners)
        for expected in ((10, 10, 8), (14, 10, 8), (14, 13, 8), (10, 13, 8)):
            self.assertIn(expected, pts)

    def test_fillet_strips_do_not_make_edges(self):
        # a quarter cylinder of radius 1 mm tessellated into 12 strips: neighbouring strips are
        # ~7.5 degrees apart, but strips of a tight fillet are too narrow to be faces
        tris = []
        n = 12
        for i in range(n):
            a0, a1 = math.pi / 2 * i / n, math.pi / 2 * (i + 1) / n
            p0 = (math.cos(a0), math.sin(a0), 0.0)
            p1 = (math.cos(a1), math.sin(a1), 0.0)
            p2 = (math.cos(a1), math.sin(a1), 20.0)
            p3 = (math.cos(a0), math.sin(a0), 20.0)
            tris += [p0, p1, p2, p0, p2, p3]
        idx, g = graph_of(subdivide(tris, 6), cell=0.4)
        self.assertEqual(len(g.edges), 0)
        self.assertEqual(len(g.corners), 0)


class RayCast(unittest.TestCase):
    def test_ray_hits_top_face_first(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        hit = idx.raycast((20.0, 15.0, 50.0), (0.0, 0.0, -1.0))
        self.assertIsNotNone(hit)
        t, tri, p = hit
        self.assertTrue(close(p, (20.0, 15.0, 20.0), 1e-6))
        self.assertAlmostEqual(t, 30.0, places=6)

    def test_oblique_ray_hits_side_face(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        d = (-1.0, 0.0, -0.25)
        hit = idx.raycast((60.0, 15.0, 15.0), d)
        self.assertIsNotNone(hit)
        p = hit[2]
        self.assertAlmostEqual(p[0], 40.0, places=6)
        self.assertAlmostEqual(p[2], 15.0 - 20.0 * 0.25, places=6)

    def test_ray_missing_the_mesh(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        self.assertIsNone(idx.raycast((100.0, 100.0, 50.0), (0.0, 0.0, -1.0)))
        self.assertIsNone(idx.raycast((20.0, 15.0, 50.0), (0.0, 0.0, 1.0)))

    def test_ray_starting_inside_box_hits_far_wall(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        hit = idx.raycast((20.0, 15.0, 10.0), (1.0, 0.0, 0.0))
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit[2][0], 40.0, places=6)


class Persistence(unittest.TestCase):
    def test_state_round_trip_uses_plain_data_only(self):
        import pickle
        idx, g = graph_of(subdivide(BOX, 6), cell=3.0)
        blob = pickle.dumps((idx.to_state(), g.to_state()), protocol=pickle.HIGHEST_PROTOCOL)
        idx_state, g_state = pickle.loads(blob)
        idx2 = MeshIndex.from_state(idx_state)
        g2 = F.FeatureGraph.from_state(g_state, idx2)
        self.assertEqual(len(g2.corners), 8)
        self.assertEqual(len(g2.edges), 12)
        c = best(g2.query((39.4, 29.5, 20.0), 1.5), 'corner')
        self.assertTrue(close(c['point'], (40, 30, 20), 1e-6))
        self.assertIsNotNone(idx2.raycast((20.0, 15.0, 50.0), (0.0, 0.0, -1.0)))

    def test_pickle_round_trip(self):
        import pickle
        idx, g = graph_of(subdivide(BOX, 6), cell=3.0)
        blob = pickle.dumps((idx, g), protocol=pickle.HIGHEST_PROTOCOL)
        idx2, g2 = pickle.loads(blob)
        g2.index = idx2
        self.assertEqual(len(g2.corners), 8)
        self.assertEqual(idx2.triangle_count, idx.triangle_count)
        c = best(g2.query((39.4, 29.5, 20.0), 1.5), 'corner')
        self.assertTrue(close(c['point'], (40, 30, 20), 1e-6))


if __name__ == '__main__':
    unittest.main()
