"""Unit tests for the world -> Setup WCS transformation.

Run with any Python 3 interpreter (no Fusion needed):

    python -m unittest discover -s FusionIPWInspector/tests

The four named cases mirror the manual verification matrix in the README:
A) WCS at world origin, B) translated, C) rotated 90 degrees, D) rotary style
setup where the setup X axis is the rotary axis and Y/Z are radial.
The "real" cases use matrices read from a real Fusion document.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.setup_transform import SetupFrame, SetupFrameError  # noqa: E402
from core.point_inspector import InspectedPoint, format_axis_value, format_machine_line  # noqa: E402


def close(a, b, tol=1e-9):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


class CaseA_WorldOrigin(unittest.TestCase):
    def test_identity_is_passthrough(self):
        f = SetupFrame.identity()
        self.assertTrue(close(f.world_to_setup((12.384, -21.75, 6.125)), (12.384, -21.75, 6.125)))
        self.assertTrue(f.is_identity())


class CaseB_Translated(unittest.TestCase):
    def test_translation_is_subtracted(self):
        # WCS at the stock top corner (22, -25.15, 11.8224) with world orientation.
        f = SetupFrame.from_matrix_rows([1, 0, 0, 22.0, 0, 1, 0, -25.15, 0, 0, 1, 11.8224, 0, 0, 0, 1])
        self.assertTrue(close(f.world_to_setup((22.0, -25.15, 11.8224)), (0, 0, 0)))
        self.assertTrue(close(f.world_to_setup((0.0, 0.0, 0.0)), (-22.0, 25.15, -11.8224)))

    def test_direction_is_world_to_setup_not_reverse(self):
        f = SetupFrame.from_matrix_rows([1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        # A point 3 mm right of the WCS origin must read X=+3, never X=+13.
        self.assertTrue(close(f.world_to_setup((13.0, 0, 0)), (3.0, 0, 0)))


class CaseC_Rotated90(unittest.TestCase):
    def test_flipped_setup_from_real_document(self):
        # Setup2 of the test document: part flipped over (Y and Z negated).
        f = SetupFrame.from_matrix_rows([1, 0, 0, 22.0, 0, -1, 0, 25.15, 0, 0, -1, -13.3776, 0, 0, 0, 1])
        self.assertTrue(close(f.world_to_setup((22.0, 25.15, -13.3776)), (0, 0, 0)))
        # 5 mm above the origin in world -Z is +5 mm in setup Z.
        self.assertTrue(close(f.world_to_setup((22.0, 25.15, -18.3776)), (0, 0, 5.0)))
        # World +Y becomes setup -Y.
        self.assertTrue(close(f.world_to_setup((22.0, 27.15, -13.3776)), (0, -2.0, 0)))

    def test_rotation_about_z_by_90(self):
        c, s = math.cos(math.pi / 2), math.sin(math.pi / 2)
        f = SetupFrame.from_matrix_rows([c, -s, 0, 0, s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        # Setup X axis points along world +Y, so world (0, 7, 0) is setup (7, 0, 0).
        self.assertTrue(close(f.world_to_setup((0, 7, 0)), (7, 0, 0), 1e-9))
        self.assertTrue(close(f.world_to_setup((4, 0, 0)), (0, -4, 0), 1e-9))


class CaseD_RotarySetup(unittest.TestCase):
    def test_rotary_setup_from_real_document(self):
        # Setup5 of the test document: setup X = world +Z, setup Y = world -Y, setup Z = world +X.
        f = SetupFrame.from_matrix_rows([0, 0, 1, 0, 0, -1, 0, 0, 1, 0, 0, -0.7776, 0, 0, 0, 1])
        self.assertTrue(close(f.world_to_setup((0, 0, -0.7776)), (0, 0, 0)))
        self.assertTrue(close(f.world_to_setup((0, 0, 9.2224)), (10.0, 0, 0)))    # along rotary axis
        self.assertTrue(close(f.world_to_setup((12.6, 0, -0.7776)), (0, 0, 12.6)))  # radial: setup +Z
        self.assertTrue(close(f.world_to_setup((0, -25.15, -0.7776)), (0, 25.15, 0)))
        self.assertEqual(f.describe(), 'origin (0, 0, -0.778) mm, X = world +Z, Y = world -Y, Z = world +X')

    def test_round_trip(self):
        f = SetupFrame.from_matrix_rows([0, 0, 1, 3, 0, -1, 0, -4, 1, 0, 0, 5, 0, 0, 0, 1])
        p = (1.234, -5.678, 9.1011)
        self.assertTrue(close(f.setup_to_world(f.world_to_setup(p)), p, 1e-12))


class Validation(unittest.TestCase):
    def test_rejects_scaled_matrix(self):
        with self.assertRaises(SetupFrameError):
            SetupFrame.from_matrix_rows([2, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])

    def test_rejects_left_handed(self):
        with self.assertRaises(SetupFrameError):
            SetupFrame.from_matrix_rows([-1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])

    def test_rejects_wrong_length(self):
        with self.assertRaises(SetupFrameError):
            SetupFrame.from_matrix_rows([1, 0, 0])

    def test_from_axes_normalises(self):
        f = SetupFrame.from_axes((0, 0, 0), (2, 0, 0), (0, 3, 0), (0, 0, 4))
        self.assertTrue(f.is_identity())


class Formatting(unittest.TestCase):
    def test_millimetre_formatting(self):
        self.assertEqual(format_axis_value(12.3844, 'mm'), '+12.384')
        self.assertEqual(format_axis_value(-21.75, 'mm'), '-21.750')
        self.assertEqual(format_axis_value(0.0004, 'mm'), '+0.000')
        self.assertEqual(format_axis_value(-0.0004, 'mm'), '+0.000')  # no negative zero

    def test_inch_formatting(self):
        self.assertEqual(format_axis_value(25.4, 'in'), '+1.0000')

    def test_machine_line(self):
        p = InspectedPoint(setup_xyz_mm=(12.3844, -21.75, 6.125), world_xyz_mm=(0, 0, 0),
                           setup_name='Setup5', source_kind='mesh')
        self.assertEqual(format_machine_line(p, 'mm'), 'X12.384 Y-21.750 Z6.125')
        self.assertEqual(p.formatted_lines('mm'), ['X  +12.384 mm', 'Y  -21.750 mm', 'Z   +6.125 mm'])


if __name__ == '__main__':
    unittest.main()
