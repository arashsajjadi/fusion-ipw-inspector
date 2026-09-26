"""Unit conventions of the Fusion API, kept in one place.

* Geometry objects returned by the Design API (Point3D, bounding boxes,
  Selection.point) are in centimetres, the API's internal unit.
* ``adsk.cam.Setup.workCoordinateSystem`` translation values are in
  millimetres (verified against real setups whose stock box dimensions are
  known; see README "Coordinate frames"). This is the one place to change if
  Autodesk ever changes that.
* The tool works in millimetres internally.
"""
from __future__ import annotations

from typing import Sequence, Tuple

API_CM_TO_MM = 10.0
WCS_MATRIX_TO_MM = 1.0   # Setup.workCoordinateSystem translation is already in mm


def api_point_to_mm(p) -> Tuple[float, float, float]:
    """Convert an adsk.core.Point3D (cm) to a millimetre tuple."""
    return (p.x * API_CM_TO_MM, p.y * API_CM_TO_MM, p.z * API_CM_TO_MM)


def mm_to_api(xyz_mm: Sequence[float]) -> Tuple[float, float, float]:
    """Convert a millimetre tuple to the API's centimetres."""
    return (xyz_mm[0] / API_CM_TO_MM, xyz_mm[1] / API_CM_TO_MM, xyz_mm[2] / API_CM_TO_MM)


def wcs_matrix_rows_mm(matrix) -> list:
    """Row-major 16 values of a Setup WCS Matrix3D with translation in mm."""
    rows = list(matrix.asArray())
    for i in (3, 7, 11):
        rows[i] *= WCS_MATRIX_TO_MM
    return rows


def document_length_unit(design) -> str:
    """The design's default length unit as one of mm/cm/m/in/ft."""
    try:
        return design.unitsManager.defaultLengthUnits or 'mm'
    except Exception:
        return 'mm'
