"""Reading a saved-stock mesh file and working out its unit and coordinate frame.

Pure Python (no Fusion API) so it can be unit tested anywhere. Fusion's
Simulation "Save Stock" writes an STL; STL carries no unit, so the file's
bounding box is compared with the machined model to decide between mm, cm and
inch, and between world coordinates and each setup's WCS.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from .transform import SetupFrame

Box = Tuple[Tuple[float, float, float], Tuple[float, float, float]]


class StockFileError(ValueError):
    """User facing failure: the message says what happened and what to do next."""


@dataclass
class StockFileInfo:
    path: str
    triangle_count: int
    bbox: Box                    # in file units
    mean_edge: float             # in file units, a proxy for mesh resolution
    is_ascii: bool


@dataclass
class StockInterpretation:
    scale_to_mm: float           # 1 (mm), 10 (cm), 25.4 (inch)
    frame: SetupFrame            # frame the file coordinates are expressed in
    score: float                 # 0..1 containment of the model inside the stock
    label: str


UNIT_CANDIDATES = ((1.0, 'mm'), (10.0, 'cm'), (25.4, 'inch'))


# ------------------------------------------------------------------ STL parsing
def read_stock_file(path: str) -> StockFileInfo:
    """Read an STL (binary or ASCII) far enough to know its bounding box."""
    with open(path, 'rb') as fh:
        data = fh.read()
    name = os.path.basename(path)
    if len(data) < 84:
        raise StockFileError('The file is too small to be a stock mesh: %s' % name)
    mins = [float('inf')] * 3
    maxs = [float('-inf')] * 3
    edge_sum = 0.0
    tri_count = 0
    is_ascii = data[:5].lower() == b'solid' and b'facet' in data[:4096]
    if not is_ascii:
        count = struct.unpack('<I', data[80:84])[0]
        if 84 + count * 50 > len(data):
            raise StockFileError('The STL file looks truncated: %s' % name)
        for tri in struct.iter_unpack('<12fH', data[84:84 + count * 50]):
            v = tri[3:12]
            for i in range(3):
                for k in (i, i + 3, i + 6):
                    c = v[k]
                    if c < mins[i]:
                        mins[i] = c
                    if c > maxs[i]:
                        maxs[i] = c
            if tri_count < 20000:  # enough triangles for a resolution estimate
                edge_sum += _dist(v[0:3], v[3:6])
            tri_count += 1
    else:
        verts = []
        for line in data.decode('ascii', errors='ignore').splitlines():
            s = line.strip()
            if s.startswith('vertex'):
                parts = s.split()
                if len(parts) >= 4:
                    p = (float(parts[1]), float(parts[2]), float(parts[3]))
                    verts.append(p)
                    for i in range(3):
                        mins[i] = min(mins[i], p[i])
                        maxs[i] = max(maxs[i], p[i])
        tri_count = len(verts) // 3
        for t in range(min(tri_count, 20000)):
            edge_sum += _dist(verts[3 * t], verts[3 * t + 1])
    if tri_count == 0:
        raise StockFileError('No triangles were found in %s. Save the stock again from Simulation.' % name)
    return StockFileInfo(path=path, triangle_count=tri_count, bbox=(tuple(mins), tuple(maxs)),
                         mean_edge=edge_sum / max(1, min(tri_count, 20000)), is_ascii=is_ascii)


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


# ------------------------------------------------------- unit / frame inference
def infer_interpretation(file_bbox: Box, model_bbox_mm: Optional[Box],
                         candidate_frames: Sequence[SetupFrame]) -> StockInterpretation:
    """Pick the (unit, frame) combination under which the stock encloses the model.

    Fusion writes Save Stock files in millimetres in world coordinates, but STL
    is unitless and we refuse to rely on that silently. Every combination of
    unit (mm, cm, inch) and frame (world, each setup WCS) is scored by how much
    of the model's bounding box lies inside the stock's bounding box; the best
    one wins and is reported to the user. Without model geometry the file is
    taken as millimetres in world space.
    """
    frames = [SetupFrame.identity('World')] + list(candidate_frames)
    if model_bbox_mm is None:
        return StockInterpretation(1.0, frames[0], 0.0, 'mm, world coordinates (not verified: setup has no model)')
    model_vol = max(box_volume(model_bbox_mm), 1e-9)
    best: Optional[StockInterpretation] = None
    best_adjusted = float('-inf')
    for scale, unit_name in UNIT_CANDIDATES:
        scaled = tuple(tuple(c * scale for c in corner) for corner in file_bbox)
        for frame in frames:
            world_box = transform_box(scaled, frame)
            score = overlap_volume(model_bbox_mm, world_box) / model_vol
            vol = box_volume(world_box)
            # Prefer the tightest stock that still contains the model (guards against
            # a cm/mm mix-up that happens to enclose everything at 1000x the volume).
            adjusted = score - 0.02 * max(0.0, (vol / model_vol) - 20.0)
            cand = StockInterpretation(scale, frame, score, '%s, %s' % (
                unit_name, 'world coordinates' if frame.is_identity() else 'coordinates of %s' % frame.name))
            if adjusted > best_adjusted:
                best, best_adjusted = cand, adjusted
    return best


def box_volume(box: Box) -> float:
    return max(0.0, box[1][0] - box[0][0]) * max(0.0, box[1][1] - box[0][1]) * max(0.0, box[1][2] - box[0][2])


def overlap_volume(a: Box, b: Box) -> float:
    lo = tuple(max(a[0][i], b[0][i]) for i in range(3))
    hi = tuple(min(a[1][i], b[1][i]) for i in range(3))
    return box_volume((lo, hi))


def transform_box(box: Box, frame: SetupFrame) -> Box:
    corners = [(x, y, z) for x in (box[0][0], box[1][0]) for y in (box[0][1], box[1][1]) for z in (box[0][2], box[1][2])]
    world = [frame.setup_to_world(c) for c in corners]
    return (tuple(min(c[i] for c in world) for i in range(3)), tuple(max(c[i] for c in world) for i in range(3)))
