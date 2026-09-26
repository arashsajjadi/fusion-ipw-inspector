"""Unit tests for the stock sanity checks and the self-test's pure checks.

    python -m unittest discover -s FusionIPWInspector/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from FusionIPWInspector.core.stock_file import StockFileInfo  # noqa: E402
from FusionIPWInspector.diagnostics.self_test import run_pure_checks  # noqa: E402
from FusionIPWInspector.stock.mesh_validation import validate_stock  # noqa: E402


def info(bbox, tris=1000, edge=0.2):
    return StockFileInfo(path='x.stl', triangle_count=tris, bbox=bbox, mean_edge=edge, is_ascii=False)


class ValidateStock(unittest.TestCase):
    model = ((0.0, 0.0, 0.0), (40.0, 30.0, 20.0))

    def test_good_stock_passes(self):
        rep = validate_stock(info(((-1, -1, 0), (41, 31, 21))), 1.0, self.model)
        self.assertTrue(rep.ok, rep.problems)
        self.assertEqual(rep.notes, [])

    def test_plain_box_is_noted_not_rejected(self):
        rep = validate_stock(info(((-1, -1, 0), (41, 31, 21)), tris=12), 1.0, self.model, previous_setup_stock=True)
        self.assertTrue(rep.ok)
        self.assertIn('unmachined box', rep.notes[0])
        self.assertIn('preceding', rep.notes[0])

    def test_wrong_unit_is_rejected(self):
        # A centimetre file read as millimetres is 10x too small.
        rep = validate_stock(info(((-0.1, -0.1, 0), (4.1, 3.1, 2.1))), 1.0, self.model)
        self.assertFalse(rep.ok)
        self.assertTrue(any('enclose' in p or 'scale' in p for p in rep.problems), rep.problems)

    def test_stock_from_other_setup_is_rejected(self):
        rep = validate_stock(info(((500, 500, 500), (540, 530, 520))), 1.0, self.model)
        self.assertFalse(rep.ok)

    def test_orientation_check_uses_part_export(self):
        good_part = ((0.0, 0.0, 0.0), (40.0, 30.0, 20.0))
        rep = validate_stock(info(((-1, -1, 0), (41, 31, 21))), 1.0, self.model, part_box=good_part)
        self.assertTrue(rep.ok, rep.problems)
        swapped = ((0.0, 0.0, 0.0), (30.0, 40.0, 20.0))   # X and Y swapped: wrong orientation
        rep = validate_stock(info(((-1, -1, 0), (41, 31, 21))), 1.0, self.model, part_box=swapped)
        self.assertFalse(rep.ok)
        self.assertIn('Orientation', rep.problems[0])

    def test_expected_box_mismatch_is_a_note(self):
        rep = validate_stock(info(((-1, -1, 0), (41, 31, 21))), 1.0, self.model, expected_stock_box=((-1, -1, 0), (61, 31, 21)))
        self.assertTrue(rep.ok)
        self.assertTrue(any('extent' in n for n in rep.notes))

    def test_empty_mesh(self):
        rep = validate_stock(info(((0, 0, 0), (0, 0, 0)), tris=0), 1.0, self.model)
        self.assertFalse(rep.ok)


class SelfTestPureChecks(unittest.TestCase):
    def test_all_pure_checks_pass(self):
        checks = run_pure_checks()
        self.assertGreaterEqual(len(checks), 8)
        self.assertTrue(all(c.ok for c in checks), [c.line() for c in checks if not c.ok])


if __name__ == '__main__':
    unittest.main()
