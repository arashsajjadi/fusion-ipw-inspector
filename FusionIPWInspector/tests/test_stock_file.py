"""Unit tests for reading saved-stock STL files and inferring their unit and frame.

Run with any Python 3 interpreter (no Fusion needed):

    python -m unittest discover -s FusionIPWInspector/tests
"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from FusionIPWInspector.core.transform import SetupFrame  # noqa: E402
from FusionIPWInspector.core.stock_file import StockFileError, infer_interpretation, read_stock_file  # noqa: E402


def write_binary_box(path, lo, hi):
    """Write a binary STL of an axis-aligned box (12 triangles)."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, 'wb') as fh:
        fh.write(b'\0' * 80 + struct.pack('<I', len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack('<12fH', 0, 0, 0, *v[a], *v[b], *v[c], 0))


def write_ascii_box(path, lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    tris = [((x0, y0, z0), (x1, y0, z0), (x1, y1, z1)), ((x0, y1, z1), (x0, y0, z0), (x1, y1, z0))]
    with open(path, 'w') as fh:
        fh.write('solid test\n')
        for tri in tris:
            fh.write(' facet normal 0 0 0\n  outer loop\n')
            for p in tri:
                fh.write('   vertex %g %g %g\n' % p)
            fh.write('  endloop\n endfacet\n')
        fh.write('endsolid test\n')


class ReadStockFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_binary_bbox(self):
        p = os.path.join(self.dir, 'box.stl')
        write_binary_box(p, (-1, -1, 0), (41, 31, 21))
        info = read_stock_file(p)
        self.assertEqual(info.triangle_count, 12)
        self.assertFalse(info.is_ascii)
        self.assertEqual(info.bbox, ((-1.0, -1.0, 0.0), (41.0, 31.0, 21.0)))
        self.assertGreater(info.mean_edge, 0)

    def test_ascii_bbox(self):
        p = os.path.join(self.dir, 'box_ascii.stl')
        write_ascii_box(p, (0, 0, 0), (4, 3, 2))
        info = read_stock_file(p)
        self.assertTrue(info.is_ascii)
        self.assertEqual(info.triangle_count, 2)
        self.assertEqual(info.bbox, ((0.0, 0.0, 0.0), (4.0, 3.0, 2.0)))

    def test_truncated_file(self):
        p = os.path.join(self.dir, 'bad.stl')
        with open(p, 'wb') as fh:
            fh.write(b'\0' * 80 + struct.pack('<I', 100) + b'\0' * 10)
        with self.assertRaises(StockFileError):
            read_stock_file(p)

    def test_tiny_file(self):
        p = os.path.join(self.dir, 'tiny.stl')
        with open(p, 'wb') as fh:
            fh.write(b'123')
        with self.assertRaises(StockFileError):
            read_stock_file(p)


class InferInterpretation(unittest.TestCase):
    model = ((0.0, 0.0, 0.0), (40.0, 30.0, 20.0))          # mm, world
    setup_b = SetupFrame.from_matrix_rows([1, 0, 0, -1, 0, 1, 0, -1, 0, 0, 1, 21, 0, 0, 0, 1], 'B')

    def test_millimetre_world_file(self):
        stock = ((-1.0, -1.0, 0.0), (41.0, 31.0, 21.0))
        r = infer_interpretation(stock, self.model, [self.setup_b])
        self.assertEqual(r.scale_to_mm, 1.0)
        self.assertTrue(r.frame.is_identity())
        self.assertAlmostEqual(r.score, 1.0)
        self.assertEqual(r.label, 'mm, world coordinates')

    def test_centimetre_file_is_detected(self):
        stock = ((-0.1, -0.1, 0.0), (4.1, 3.1, 2.1))
        r = infer_interpretation(stock, self.model, [self.setup_b])
        self.assertEqual(r.scale_to_mm, 10.0)
        self.assertTrue(r.frame.is_identity())
        self.assertEqual(r.label, 'cm, world coordinates')

    def test_inch_file_is_detected(self):
        stock = tuple(tuple(c / 25.4 for c in corner) for corner in ((-1.0, -1.0, 0.0), (41.0, 31.0, 21.0)))
        r = infer_interpretation(stock, self.model, [self.setup_b])
        self.assertEqual(r.scale_to_mm, 25.4)

    def test_file_in_setup_frame_is_detected(self):
        # Stock expressed in setup B coordinates: WCS origin at world (-1,-1,21), so the
        # world box (-1..41, -1..31, 0..21) becomes (0..42, 0..32, -21..0).
        stock = ((0.0, 0.0, -21.0), (42.0, 32.0, 0.0))
        r = infer_interpretation(stock, self.model, [self.setup_b])
        self.assertEqual(r.scale_to_mm, 1.0)
        self.assertEqual(r.frame.name, 'B')
        self.assertAlmostEqual(r.score, 1.0)
        self.assertEqual(r.label, 'mm, coordinates of B')

    def test_wrong_file_scores_low(self):
        stock = ((500.0, 500.0, 500.0), (510.0, 510.0, 510.0))
        r = infer_interpretation(stock, self.model, [self.setup_b])
        self.assertLess(r.score, 0.5)

    def test_without_model_defaults_to_mm_world(self):
        r = infer_interpretation(((0, 0, 0), (1, 1, 1)), None, [self.setup_b])
        self.assertEqual(r.scale_to_mm, 1.0)
        self.assertTrue(r.frame.is_identity())


if __name__ == '__main__':
    unittest.main()
