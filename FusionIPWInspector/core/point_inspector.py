"""Turning a picked world point into a Setup-WCS reading and formatting it.

Pure Python: no Fusion API imports, so the formatting rules are unit tested
outside Fusion. Lengths are millimetres internally and converted to the
display unit only when formatted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .transform import SetupFrame, Vec3

# Display unit -> (millimetres per unit, decimals shown). Decimals reflect what
# a machinist can use: 1 um in metric, 0.1 thou in inches. They deliberately
# do not go finer, because the mesh sources are not better than that anyway.
UNIT_TABLE = {
    'mm': (1.0, 3),
    'cm': (10.0, 4),
    'm': (1000.0, 6),
    'in': (25.4, 4),
    'ft': (304.8, 5),
}

# Kinds of geometry a point can be picked on. Mesh sources are approximate
# because they come from a tessellated / simulated surface.
SOURCE_LABELS = {
    'ipw': 'IPW',
    'mesh': 'mesh',
    'model': 'model',
    'brep': 'model',
    'point': 'point',
}
APPROXIMATE_KINDS = ('ipw', 'mesh')


def normalize_unit(unit: str) -> str:
    unit = (unit or 'mm').strip().lower()
    aliases = {'millimeter': 'mm', 'millimeters': 'mm', 'millimetre': 'mm', 'inch': 'in',
               'inches': 'in', 'centimeter': 'cm', 'centimeters': 'cm', 'meter': 'm',
               'meters': 'm', 'foot': 'ft', 'feet': 'ft'}
    unit = aliases.get(unit, unit)
    return unit if unit in UNIT_TABLE else 'mm'


def decimals_for(unit: str) -> int:
    return UNIT_TABLE[normalize_unit(unit)][1]


def mm_to_unit(value_mm: float, unit: str) -> float:
    return value_mm / UNIT_TABLE[normalize_unit(unit)][0]


def format_axis_value(value_mm: float, unit: str, decimals: Optional[int] = None) -> str:
    """Format one coordinate with an explicit sign, e.g. '+12.384'.

    Rounds to the unit's machinist precision and never prints '-0.000'.
    """
    unit = normalize_unit(unit)
    if decimals is None:
        decimals = decimals_for(unit)
    v = round(mm_to_unit(value_mm, unit), decimals)
    if v == 0:
        v = 0.0
    return ('%+.' + str(decimals) + 'f') % v


def format_machine_line(point: 'InspectedPoint', unit: str, decimals: Optional[int] = None) -> str:
    """Controller style line without units or plus signs: 'X12.384 Y-21.750 Z6.125'."""
    parts = []
    for axis, v in zip('XYZ', point.setup_xyz_mm):
        s = format_axis_value(v, unit, decimals)
        parts.append(axis + (s[1:] if s.startswith('+') else s))
    return ' '.join(parts)


@dataclass
class InspectedPoint:
    """One picked point, expressed both in world space and in a setup WCS (mm)."""

    setup_xyz_mm: Vec3
    world_xyz_mm: Vec3
    setup_name: str
    source_kind: str = 'ipw'           # 'ipw' | 'mesh' | 'model' | 'point'
    source_name: str = ''              # e.g. body name
    notes: List[str] = field(default_factory=list)

    @classmethod
    def from_world(cls, world_xyz_mm: Vec3, frame: SetupFrame, source_kind: str = 'mesh',
                   source_name: str = '') -> 'InspectedPoint':
        return cls(setup_xyz_mm=frame.world_to_setup(world_xyz_mm), world_xyz_mm=tuple(world_xyz_mm),
                   setup_name=frame.name, source_kind=source_kind, source_name=source_name)

    # ------------------------------------------------------------ formatting
    def formatted_lines(self, unit: str, decimals: Optional[int] = None) -> List[str]:
        unit = normalize_unit(unit)
        vals = [format_axis_value(v, unit, decimals) for v in self.setup_xyz_mm]
        width = max(len(v) for v in vals)
        return ['%s  %s %s' % (axis, v.rjust(width), unit) for axis, v in zip('XYZ', vals)]

    def clipboard_text(self, unit: str, decimals: Optional[int] = None) -> str:
        """Tab separated 'X<tab>Y<tab>Z' numbers, convenient for spreadsheets and notes."""
        return '\t'.join(format_axis_value(v, unit, decimals).lstrip('+') for v in self.setup_xyz_mm)

    def machine_text(self, unit: str, decimals: Optional[int] = None) -> str:
        return format_machine_line(self, unit, decimals)

    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_kind, self.source_kind)

    def is_approximate(self) -> bool:
        return self.source_kind in APPROXIMATE_KINDS


def delta(a: InspectedPoint, b: InspectedPoint) -> Tuple[Vec3, float]:
    """Difference b - a in the setup frame (mm) and the straight-line distance."""
    d = tuple(bb - aa for aa, bb in zip(a.setup_xyz_mm, b.setup_xyz_mm))
    dist = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
    return d, dist
