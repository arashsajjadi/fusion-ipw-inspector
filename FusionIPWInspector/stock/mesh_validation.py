"""Sanity checks that decide whether a stock mesh can be trusted.

Pure Python (no Fusion API) so the rules are unit tested. Everything is in
millimetres in the frame the mesh file is expressed in (a setup WCS or world).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..core.stock_file import Box, StockFileInfo, box_volume, overlap_volume

PLAIN_BOX_TRIANGLES = 12
# How far a model may poke out of its stock before the stock is called wrong.
CONTAINMENT_TOLERANCE_MM = 0.5
# Model bounding box extents must match the part export within this much (per axis).
ORIENTATION_TOLERANCE_MM = 0.2


@dataclass
class ValidationReport:
    ok: bool = True
    problems: List[str] = field(default_factory=list)   # user facing, blocking
    notes: List[str] = field(default_factory=list)      # user facing, informational

    def add_problem(self, text: str) -> None:
        self.ok = False
        self.problems.append(text)


def extents(box: Box):
    return tuple(box[1][i] - box[0][i] for i in range(3))


def validate_stock(stock: StockFileInfo, unit_scale_mm: float, model_box: Optional[Box],
                   expected_stock_box: Optional[Box] = None, part_box: Optional[Box] = None,
                   previous_setup_stock: bool = False) -> ValidationReport:
    """Check a stock mesh against what is known about the setup.

    ``model_box`` is the setup model's bounding box in the same frame as the
    mesh (mm). ``expected_stock_box`` is Fusion's own stock box for the setup
    (mm, same frame) when known. ``part_box`` is the bounding box of the part
    export that came with the stock, if any.
    """
    rep = ValidationReport()
    scaled = tuple(tuple(c * unit_scale_mm for c in corner) for corner in stock.bbox)
    ex = extents(scaled)
    if stock.triangle_count <= 0 or min(ex) <= 0:
        rep.add_problem('The stock mesh is empty.')
        return rep
    if stock.triangle_count <= PLAIN_BOX_TRIANGLES:
        rep.notes.append('The stock is an unmachined box.' + (
            ' Generate the preceding setup\'s toolpaths to get the machined in-process stock.'
            if previous_setup_stock else ''))
    if model_box is not None:
        mvol = box_volume(model_box)
        if mvol > 0:
            score = overlap_volume(model_box, scaled) / mvol
            if score < 0.5:
                rep.add_problem('it does not enclose the model (%d%% overlap), so it is probably from another '
                                'setup or design.' % round(score * 100))
            elif score < 0.98:
                rep.notes.append('The model pokes out of the stock (%d%% enclosed).' % round(score * 100))
            # Scale sanity: stock more than 20x or less than 1/20 of the model is a unit mix-up.
            mext = extents(model_box)
            ratio = max(ex) / max(1e-9, max(mext))
            if ratio > 20 or ratio < 0.05:
                rep.add_problem('scale mismatch: the model spans about %.1f mm but the stock spans %.1f mm '
                                '(wrong file unit?).' % (max(mext), max(ex)))
    if expected_stock_box is not None:
        eex = extents(expected_stock_box)
        for axis, (got, want) in enumerate(zip(ex, eex)):
            if want > 0 and abs(got - want) > max(1.0, 0.05 * want):
                rep.notes.append('Stock %s extent is %.2f mm, setup stock box says %.2f mm.' % ('XYZ'[axis], got, want))
    if part_box is not None and model_box is not None:
        for axis in range(3):
            got = part_box[1][axis] - part_box[0][axis]
            want = model_box[1][axis] - model_box[0][axis]
            if abs(got - want) > ORIENTATION_TOLERANCE_MM:
                rep.add_problem('Orientation check failed on %s: exported part spans %.3f mm, model spans %.3f mm.'
                                % ('XYZ'[axis], got, want))
                break
        else:
            shift = max(abs(part_box[0][i] - model_box[0][i]) for i in range(3))
            if shift > ORIENTATION_TOLERANCE_MM:
                rep.add_problem('Placement check failed: exported part is offset by %.3f mm from the model.' % shift)
    return rep
