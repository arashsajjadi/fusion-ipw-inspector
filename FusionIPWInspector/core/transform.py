"""Coordinate transformation between Fusion world space and a Manufacturing Setup WCS.

This module is intentionally free of any Fusion API dependency so the math can
be unit tested with a plain Python interpreter. The Fusion side only has to
hand over the 16 numbers of ``adsk.cam.Setup.workCoordinateSystem``.

Conventions
-----------
* ``Setup.workCoordinateSystem`` is a 4x4 matrix in row-major order whose
  three rotation columns are the setup X, Y and Z axes expressed in world
  space and whose fourth column is the WCS origin in world space. In other
  words it maps *setup* coordinates to *world* coordinates.
* This tool needs the opposite direction (world -> setup), so the inverse of
  that rigid transform is used: ``p_setup = R^T * (p_world - origin)``.
* All lengths handled here are in millimetres. Callers convert from whatever
  unit the Fusion API gave them before calling in (see ``fusion_units``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

Vec3 = Tuple[float, float, float]

_ORTHONORMAL_TOLERANCE = 1e-6


class SetupFrameError(ValueError):
    """Raised when a WCS matrix cannot be interpreted as a rigid transform."""


@dataclass(frozen=True)
class SetupFrame:
    """A Manufacturing Setup work coordinate system (WCS) in world space.

    ``axis_x``, ``axis_y`` and ``axis_z`` are unit vectors in world space and
    ``origin`` is the WCS origin in world space, all in millimetres.
    """

    origin: Vec3
    axis_x: Vec3
    axis_y: Vec3
    axis_z: Vec3
    name: str = ''

    # ------------------------------------------------------------------ build
    @classmethod
    def from_matrix_rows(cls, values: Sequence[float], name: str = '') -> 'SetupFrame':
        """Build a frame from the 16 row-major values of a Fusion Matrix3D.

        The matrix is validated to be a proper rigid transform (orthonormal,
        right-handed rotation, no scale). Anything else raises SetupFrameError
        instead of silently producing wrong coordinates.
        """
        if len(values) != 16:
            raise SetupFrameError('expected 16 matrix values, got %d' % len(values))
        m = [float(v) for v in values]
        ax = (m[0], m[4], m[8])
        ay = (m[1], m[5], m[9])
        az = (m[2], m[6], m[10])
        origin = (m[3], m[7], m[11])
        bottom = (m[12], m[13], m[14], m[15])
        if any(abs(b - e) > 1e-9 for b, e in zip(bottom, (0.0, 0.0, 0.0, 1.0))):
            raise SetupFrameError('matrix is not an affine transform (bad last row)')
        for v in (ax, ay, az):
            if abs(_norm(v) - 1.0) > _ORTHONORMAL_TOLERANCE:
                raise SetupFrameError('WCS axes are not unit length (scaled matrix?)')
        if (abs(_dot(ax, ay)) > _ORTHONORMAL_TOLERANCE or abs(_dot(ay, az)) > _ORTHONORMAL_TOLERANCE
                or abs(_dot(ax, az)) > _ORTHONORMAL_TOLERANCE):
            raise SetupFrameError('WCS axes are not perpendicular')
        if _dot(_cross(ax, ay), az) < 0:
            raise SetupFrameError('WCS is left-handed; refusing to guess')
        return cls(origin=origin, axis_x=ax, axis_y=ay, axis_z=az, name=name)

    @classmethod
    def from_axes(cls, origin: Vec3, axis_x: Vec3, axis_y: Vec3, axis_z: Vec3, name: str = '') -> 'SetupFrame':
        """Build a frame from explicit axes (world space). Axes are normalised."""
        rows = [
            axis_x[0], axis_y[0], axis_z[0], origin[0],
            axis_x[1], axis_y[1], axis_z[1], origin[1],
            axis_x[2], axis_y[2], axis_z[2], origin[2],
            0.0, 0.0, 0.0, 1.0,
        ]
        nx, ny, nz = _normalized(axis_x), _normalized(axis_y), _normalized(axis_z)
        rows[0], rows[4], rows[8] = nx
        rows[1], rows[5], rows[9] = ny
        rows[2], rows[6], rows[10] = nz
        return cls.from_matrix_rows(rows, name)

    @classmethod
    def identity(cls, name: str = 'World') -> 'SetupFrame':
        return cls(origin=(0.0, 0.0, 0.0), axis_x=(1.0, 0.0, 0.0),
                   axis_y=(0.0, 1.0, 0.0), axis_z=(0.0, 0.0, 1.0), name=name)

    # -------------------------------------------------------------- transform
    def world_to_setup(self, p: Vec3) -> Vec3:
        """Express a world-space point (mm) in this setup's WCS (mm)."""
        d = (p[0] - self.origin[0], p[1] - self.origin[1], p[2] - self.origin[2])
        return (_dot(d, self.axis_x), _dot(d, self.axis_y), _dot(d, self.axis_z))

    def setup_to_world(self, p: Vec3) -> Vec3:
        """Express a WCS point (mm) in world space (mm)."""
        return (
            self.origin[0] + p[0] * self.axis_x[0] + p[1] * self.axis_y[0] + p[2] * self.axis_z[0],
            self.origin[1] + p[0] * self.axis_x[1] + p[1] * self.axis_y[1] + p[2] * self.axis_z[1],
            self.origin[2] + p[0] * self.axis_x[2] + p[1] * self.axis_y[2] + p[2] * self.axis_z[2],
        )

    def world_vector_to_setup(self, v: Vec3) -> Vec3:
        """Rotate a direction vector from world space into the WCS (no translation)."""
        return (_dot(v, self.axis_x), _dot(v, self.axis_y), _dot(v, self.axis_z))

    def setup_vector_to_world(self, v: Vec3) -> Vec3:
        """Rotate a direction vector from the WCS into world space (no translation)."""
        return (
            v[0] * self.axis_x[0] + v[1] * self.axis_y[0] + v[2] * self.axis_z[0],
            v[0] * self.axis_x[1] + v[1] * self.axis_y[1] + v[2] * self.axis_z[1],
            v[0] * self.axis_x[2] + v[1] * self.axis_y[2] + v[2] * self.axis_z[2],
        )

    def matrix_rows(self) -> list:
        """The 16 row-major values of the setup->world matrix (for round trips)."""
        return [
            self.axis_x[0], self.axis_y[0], self.axis_z[0], self.origin[0],
            self.axis_x[1], self.axis_y[1], self.axis_z[1], self.origin[1],
            self.axis_x[2], self.axis_y[2], self.axis_z[2], self.origin[2],
            0.0, 0.0, 0.0, 1.0,
        ]

    def is_identity(self, tol: float = 1e-9) -> bool:
        return all(abs(a - b) < tol for a, b in zip(self.matrix_rows(),
                                                   SetupFrame.identity().matrix_rows()))

    def describe(self) -> str:
        """Short human readable summary, e.g. 'origin (22, -25.15, 11.82) mm, Z = world +X'."""
        return 'origin (%s) mm, X = %s, Y = %s, Z = %s' % (
            ', '.join(_fmt(c) for c in self.origin),
            _axis_name(self.axis_x), _axis_name(self.axis_y), _axis_name(self.axis_z))


# ---------------------------------------------------------------------- helpers
def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _normalized(a: Iterable[float]) -> Vec3:
    a = tuple(float(x) for x in a)
    n = _norm(a)
    if n == 0:
        raise SetupFrameError('zero-length axis')
    return (a[0] / n, a[1] / n, a[2] / n)


def _fmt(v: float) -> str:
    text = ('%.3f' % v).rstrip('0').rstrip('.')
    return '0' if text in ('', '-0', '0', '-') else text


def _axis_name(v: Vec3) -> str:
    names = ('X', 'Y', 'Z')
    for i in range(3):
        if abs(abs(v[i]) - 1.0) < 1e-6:
            return ('world +' if v[i] > 0 else 'world -') + names[i]
    return '(%s)' % ', '.join(_fmt(c) for c in v)
