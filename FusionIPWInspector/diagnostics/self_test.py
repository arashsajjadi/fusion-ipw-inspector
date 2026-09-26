"""Self test: transform math, unit conversion, setup WCS validity and the loaded stock.

Runs from Advanced > Diagnostics > Run self test or from the hidden command
"IPW Inspector Self Test". Pure checks live in ``run_pure_checks`` so they can
also run outside Fusion; the Fusion-dependent checks add setup and stock data.
"""
from __future__ import annotations

import math
import os
from typing import List, Optional, Tuple

from ..core.point_inspector import format_axis_value, mm_to_unit
from ..core.stock_file import read_stock_file
from ..core.transform import SetupFrame, SetupFrameError

TOLERANCE_MM = 0.005          # analytic checks
ORIENTATION_TOLERANCE_MM = 0.2  # part export vs model bounding box


class Check:
    def __init__(self, name: str, ok: bool, detail: str = '') -> None:
        self.name, self.ok, self.detail = name, ok, detail

    def line(self) -> str:
        return '%s %s%s' % ('PASS' if self.ok else 'FAIL', self.name, (' - ' + self.detail) if self.detail else '')


def _close(a, b, tol=TOLERANCE_MM) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def run_pure_checks() -> List[Check]:
    checks: List[Check] = []
    # A: identity
    f = SetupFrame.identity()
    checks.append(Check('transform A (world origin)', _close(f.world_to_setup((12.384, -21.75, 6.125)), (12.384, -21.75, 6.125))))
    # B: translated
    f = SetupFrame.from_matrix_rows([1, 0, 0, 22.0, 0, 1, 0, -25.15, 0, 0, 1, 11.8224, 0, 0, 0, 1])
    checks.append(Check('transform B (translated)', _close(f.world_to_setup((-0.04, 0.684, 10.0)), (-22.04, 25.834, -1.8224))))
    # C: flipped
    f = SetupFrame.from_matrix_rows([1, 0, 0, 22.0, 0, -1, 0, 25.15, 0, 0, -1, -13.3776, 0, 0, 0, 1])
    checks.append(Check('transform C (flipped axes)', _close(f.world_to_setup((-0.04, 0.684, 10.0)), (-22.04, 24.466, -23.3776))))
    # C2: rotated 90 about Z
    c, s = math.cos(math.pi / 2), math.sin(math.pi / 2)
    f = SetupFrame.from_matrix_rows([c, -s, 0, 0, s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
    checks.append(Check('transform C2 (rotated 90 deg)', _close(f.world_to_setup((0, 7, 0)), (7, 0, 0)) and _close(f.world_to_setup((4, 0, 0)), (0, -4, 0))))
    # D: rotary
    f = SetupFrame.from_matrix_rows([0, 0, 1, 0, 0, -1, 0, 0, 1, 0, 0, -0.7776, 0, 0, 0, 1])
    checks.append(Check('transform D (rotary X = world +Z)', _close(f.world_to_setup((-0.04, 0.684, 10.0)), (10.7776, -0.684, -0.04))))
    # round trip
    p = (1.234, -5.678, 9.1011)
    checks.append(Check('transform round trip', _close(f.setup_to_world(f.world_to_setup(p)), p, 1e-9)))
    # validation of bad matrices
    bad = 0
    for rows in ([2, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], [-1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]):
        try:
            SetupFrame.from_matrix_rows(rows)
        except SetupFrameError:
            bad += 1
    checks.append(Check('rejects scaled / mirrored WCS', bad == 2))
    # units
    checks.append(Check('unit conversion', abs(mm_to_unit(25.4, 'in') - 1.0) < 1e-12 and format_axis_value(25.4, 'in') == '+1.0000'
                        and format_axis_value(-0.0004, 'mm') == '+0.000'))
    return checks


def run_fusion_checks(setups: List[Tuple[str, List[float], int]], stock: Optional[dict]) -> List[Check]:
    """Checks that need data from Fusion.

    ``setups``: [(name, 16 matrix values in mm, operation count)]
    ``stock``: None or a dict with keys stock_path, part_path, unit_scale_mm,
    frame (SetupFrame), model_box_world_mm ((lo),(hi)), expected_box (setup frame) or None.
    """
    checks: List[Check] = []
    for name, rows, ops in setups:
        try:
            frame = SetupFrame.from_matrix_rows(rows, name)
            checks.append(Check('setup WCS valid: %s' % name, True, frame.describe()))
        except SetupFrameError as exc:
            checks.append(Check('setup WCS valid: %s' % name, False, str(exc)))
    if not stock:
        checks.append(Check('stock loaded', False, 'no in-process stock loaded; open IPW Inspector first'))
        return checks
    frame: SetupFrame = stock['frame']
    scale = stock['unit_scale_mm']
    try:
        info = read_stock_file(stock['stock_path'])
    except Exception as exc:
        checks.append(Check('stock file readable', False, str(exc)))
        return checks
    box = tuple(tuple(c * scale for c in corner) for corner in info.bbox)
    ext = tuple(box[1][i] - box[0][i] for i in range(3))
    checks.append(Check('stock file readable', True, '%d triangles, extents %.2f x %.2f x %.2f mm' % ((info.triangle_count,) + ext)))
    model_box = stock.get('model_box_world_mm')
    if model_box:
        corners = [(x, y, z) for x in (model_box[0][0], model_box[1][0]) for y in (model_box[0][1], model_box[1][1])
                   for z in (model_box[0][2], model_box[1][2])]
        local = [frame.world_to_setup(c) for c in corners]
        mbox = (tuple(min(c[i] for c in local) for i in range(3)), tuple(max(c[i] for c in local) for i in range(3)))
        mext = tuple(mbox[1][i] - mbox[0][i] for i in range(3))
        ratio = max(ext) / max(1e-9, max(mext))
        checks.append(Check('stock scale plausible', 0.05 <= ratio <= 20,
                            'stock spans %.1f mm, model spans %.1f mm' % (max(ext), max(mext))))
        inside = all(box[0][i] <= mbox[0][i] + 0.5 and box[1][i] >= mbox[1][i] - 0.5 for i in range(3))
        checks.append(Check('stock encloses model', inside, 'stock %s..%s, model %s..%s (setup frame, mm)' % (
            _fmt(box[0]), _fmt(box[1]), _fmt(mbox[0]), _fmt(mbox[1]))))
        part_path = stock.get('part_path')
        if part_path and os.path.isfile(part_path):
            try:
                pinfo = read_stock_file(part_path)
                pbox = tuple(tuple(c * scale for c in corner) for corner in pinfo.bbox)
                dev = max(max(abs(pbox[0][i] - mbox[0][i]), abs(pbox[1][i] - mbox[1][i])) for i in range(3))
                checks.append(Check('stock orientation (exported part matches model)', dev <= ORIENTATION_TOLERANCE_MM,
                                    'largest bounding-box deviation %.3f mm' % dev))
            except Exception as exc:
                checks.append(Check('stock orientation (exported part matches model)', False, str(exc)))
    expected = stock.get('expected_box')
    if expected:
        # A machined stock must fit inside Fusion's stock box for the setup and keep most of its size.
        overflow = max(max(expected[0][i] - box[0][i], box[1][i] - expected[1][i]) for i in range(3))
        eext = tuple(expected[1][i] - expected[0][i] for i in range(3))
        shrink = min(ext[i] / eext[i] if eext[i] > 0 else 1.0 for i in range(3))
        checks.append(Check('stock fits the setup stock box', overflow <= 0.5 and shrink >= 0.25,
                            'sticks out %.2f mm, smallest extent ratio %.2f' % (max(overflow, 0.0), shrink)))
    return checks


def _fmt(v) -> str:
    return '(%s)' % ', '.join('%.2f' % c for c in v)


def report_text(checks: List[Check]) -> str:
    failed = [c for c in checks if not c.ok]
    head = 'All %d checks passed.' % len(checks) if not failed else '%d of %d checks failed.' % (len(failed), len(checks))
    return head + '\n\n' + '\n'.join(c.line() for c in checks)
