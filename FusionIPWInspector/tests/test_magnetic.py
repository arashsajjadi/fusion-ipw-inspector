"""Deterministic tests for the magnetic tracker and the keyed local candidates.

    python -m unittest discover -s FusionIPWInspector/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from FusionIPWInspector.core import snap as S  # noqa: E402
from test_snap import BOX, index_of, subdivide  # noqa: E402


def cand(kind, key, distance, point=(0.0, 0.0, 0.0)):
    label = {S.CORNER: 'CORNER', S.EDGE: 'EDGE', S.SURFACE: 'SURFACE', S.RAW: 'POINT'}[kind]
    return S.Candidate(kind, point, [], 0.0, 1.0, distance, label=label, key=key)


def ranked(*cands):
    return sorted(cands, key=lambda c: (-c.priority, c.distance_from_hit))


class MagneticTrackerRules(unittest.TestCase):
    def setUp(self):
        self.t = S.MagneticTracker(retain_factor=2.0, switch_factor=0.5)
        self.raw = cand(S.RAW, ('raw',), 0.0)

    def test_acquires_corner_within_tolerance(self):
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 0.8), cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
        self.assertEqual(c.key, ('c', 1))

    def test_far_corner_is_not_acquired(self):
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 1.5), cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
        self.assertEqual(c.kind, S.SURFACE)

    def test_acquired_corner_is_retained_up_to_two_tolerances(self):
        self.t.update(ranked(cand(S.CORNER, ('c', 1), 0.5), cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
        for d in (0.9, 1.3, 1.8, 1.99):
            c = self.t.update(ranked(cand(S.CORNER, ('c', 1), d), cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
            self.assertEqual(c.key, ('c', 1), 'still held at %.2f' % d)
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 2.2), cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
        self.assertEqual(c.kind, S.SURFACE)

    def test_no_alternation_on_tiny_motion(self):
        # an edge and a surface both nearby: once the edge is acquired it stays while the cursor wobbles
        kinds = []
        for d in (0.6, 0.7, 0.9, 1.1, 0.95, 1.3, 0.8):
            c = self.t.update(ranked(cand(S.EDGE, ('e', 1), d), cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
            kinds.append(c.kind)
        self.assertEqual(kinds, [S.EDGE] * 7)

    def test_higher_priority_takes_over_within_tolerance(self):
        self.t.update(ranked(cand(S.EDGE, ('e', 1), 0.2), cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 0.9), cand(S.EDGE, ('e', 1), 0.1),
                                 cand(S.SURFACE, ('p', 1), 0.0), self.raw), 1.0)
        self.assertEqual(c.kind, S.CORNER)
        # ...but not while the corner is still outside the acquire radius
        self.t.reset()
        self.t.update(ranked(cand(S.EDGE, ('e', 1), 0.2), self.raw), 1.0)
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 1.4), cand(S.EDGE, ('e', 1), 0.1), self.raw), 1.0)
        self.assertEqual(c.kind, S.EDGE)

    def test_same_priority_switch_needs_clear_margin(self):
        self.t.update(ranked(cand(S.EDGE, ('e', 1), 0.3), self.raw), 1.0)
        # a second edge only slightly closer: keep the first
        c = self.t.update(ranked(cand(S.EDGE, ('e', 2), 0.5), cand(S.EDGE, ('e', 1), 0.7), self.raw), 1.0)
        self.assertEqual(c.key, ('e', 1))
        # clearly closer: switch
        c = self.t.update(ranked(cand(S.EDGE, ('e', 2), 0.2), cand(S.EDGE, ('e', 1), 1.2), self.raw), 1.0)
        self.assertEqual(c.key, ('e', 2))

    def test_manual_cycle_is_sticky(self):
        self.t.update(ranked(cand(S.CORNER, ('c', 1), 0.3), cand(S.EDGE, ('e', 1), 0.2), self.raw), 1.0)
        chosen = self.t.cycle()
        self.assertEqual(chosen.kind, S.EDGE)
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 0.25), cand(S.EDGE, ('e', 1), 0.3), self.raw), 1.0)
        self.assertEqual(c.key, ('e', 1))
        self.assertTrue(self.t.manual)
        # gone out of reach: automatic again
        c = self.t.update(ranked(cand(S.CORNER, ('c', 2), 0.4), self.raw), 1.0)
        self.assertEqual(c.key, ('c', 2))
        self.assertFalse(self.t.manual)

    def test_fallback_when_nothing_is_within_tolerance(self):
        # a surface 1.5 tolerances away is not "under" the cursor: the raw hit is used
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 3.0), cand(S.SURFACE, ('p', 1), 1.5), self.raw), 1.0)
        self.assertEqual(c.kind, S.RAW)
        c = self.t.update(ranked(cand(S.CORNER, ('c', 1), 3.0), cand(S.SURFACE, ('p', 1), 0.1), self.raw), 1.0)
        self.assertEqual(c.kind, S.SURFACE)


class KeyedLocalCandidates(unittest.TestCase):
    def test_same_corner_from_two_hits_has_the_same_key(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        a = S.keyed(S.snap(idx, (39.4, 29.5, 19.6), 1.0))
        b = S.keyed(S.snap(idx, (39.6, 29.3, 19.7), 1.0))
        ka = [c.key for c in a if c.kind == S.CORNER]
        kb = [c.key for c in b if c.kind == S.CORNER]
        self.assertTrue(ka and kb)
        self.assertEqual(ka[0], kb[0])
        ea = {c.key for c in a if c.kind == S.EDGE}
        eb = {c.key for c in b if c.kind == S.EDGE}
        self.assertTrue(ea & eb)

    def test_every_candidate_gets_a_key(self):
        idx = index_of(subdivide(BOX, 8), cell=3.0)
        for c in S.keyed(S.snap(idx, (20.0, 15.0, 20.0), 1.0)):
            self.assertIsNotNone(c.key)


if __name__ == '__main__':
    unittest.main()
