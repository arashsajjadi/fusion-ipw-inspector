"""Viewport feedback: a picked-point cross with a label, and a small WCS triad.

Everything is drawn with Fusion custom graphics, which are display-only: they
never touch the design, are not saved with the document and vanish when the
command closes or the add-in stops.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

import adsk.core
import adsk.fusion

from ..core.setup_transform import SetupFrame
from ..utils import log
from ..utils.fusion_units import mm_to_api

ACCENT = (255, 122, 0)
AXIS_COLORS = {'X': (220, 40, 40), 'Y': (40, 170, 40), 'Z': (40, 90, 230)}
CROSS_PIXELS = 14            # half-length of the pick cross on screen
CROSSHAIR_ICON = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources', 'marker', 'crosshair.png')
TRIAD_LENGTH_MM = 12.0       # triad axis length in model space (mm)
LABEL_PIXELS = 11


class Markers:
    """Owns at most one custom graphics group per kind (point, triad)."""

    def __init__(self, graphics_groups: adsk.fusion.CustomGraphicsGroups, model_size_mm: float = 50.0) -> None:
        self._groups = graphics_groups
        self._point_group: Optional[adsk.fusion.CustomGraphicsGroup] = None
        self._triad_group: Optional[adsk.fusion.CustomGraphicsGroup] = None
        # Text and triad sizes follow the part so they look the same on a watch case and a fixture plate.
        self.label_size_cm = max(0.08, min(1.0, model_size_mm * 0.02 / 10.0))
        self.triad_length_mm = max(3.0, min(60.0, model_size_mm * 0.2))

    # ----------------------------------------------------------------- point
    def show_point(self, world_xyz_mm: Sequence[float], label: str = '', size_mm: float = 3.0) -> None:
        """Mark a world point with a screen-sized crosshair bitmap plus a small 3D cross.

        The bitmap keeps a constant size on screen at any zoom; the 3D cross
        (``size_mm`` half-length) gives a depth cue when orbiting.
        """
        self.clear_point()
        try:
            group = self._groups.add()
            cx, cy, cz = mm_to_api(world_xyz_mm)
            anchor = adsk.core.Point3D.create(cx, cy, cz)
            half = size_mm / 10.0
            coords = adsk.fusion.CustomGraphicsCoordinates.create([
                cx - half, cy, cz, cx + half, cy, cz,
                cx, cy - half, cz, cx, cy + half, cz,
                cx, cy, cz - half, cx, cy, cz + half,
            ])
            lines = group.addLines(coords, [0, 1, 2, 3, 4, 5], False)
            lines.color = _solid(ACCENT)
            lines.weight = 2.0
            lines.depthPriority = 1
            icon = group.addPointSet(adsk.fusion.CustomGraphicsCoordinates.create([cx, cy, cz]), [0],
                                     adsk.fusion.CustomGraphicsPointTypes.UserDefinedCustomGraphicsPointType,
                                     CROSSHAIR_ICON)
            icon.depthPriority = 1
            if label:
                _add_label(group, label, anchor, ACCENT, self.label_size_cm)
            self._point_group = group
            _refresh()
            log.info('point marker drawn at %s cm (group entities: %d, icon exists: %s)' % (
                (round(cx, 4), round(cy, 4), round(cz, 4)), group.count, os.path.isfile(CROSSHAIR_ICON)))
        except Exception as exc:  # graphics are cosmetic; never block the reading
            log.error('could not draw point marker', exc)

    def clear_point(self) -> None:
        self._point_group = _delete(self._point_group)
        _refresh()

    # ----------------------------------------------------------------- triad
    def show_triad(self, frame: SetupFrame) -> None:
        self.clear_triad()
        try:
            group = self._groups.add()
            o = frame.origin
            origin = adsk.core.Point3D.create(*mm_to_api(o))
            for name, axis in (('X', frame.axis_x), ('Y', frame.axis_y), ('Z', frame.axis_z)):
                tip = tuple(o[i] + axis[i] * self.triad_length_mm for i in range(3))
                ox, oy, oz = mm_to_api(o)
                tx, ty, tz = mm_to_api(tip)
                line = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create([ox, oy, oz, tx, ty, tz]),
                                      [0, 1], False)
                line.color = _solid(AXIS_COLORS[name])
                line.weight = 2.0
                line.depthPriority = 1
                _add_label(group, name, adsk.core.Point3D.create(tx, ty, tz), AXIS_COLORS[name], self.label_size_cm)
            dot = group.addPointSet(adsk.fusion.CustomGraphicsCoordinates.create([origin.x, origin.y, origin.z]), [0],
                                    adsk.fusion.CustomGraphicsPointTypes.PointCloudCustomGraphicsPointType, '')
            dot.color = _solid((255, 255, 255))
            dot.depthPriority = 1
            self._triad_group = group
            _refresh()
        except Exception as exc:
            log.error('could not draw WCS triad', exc)

    def clear_triad(self) -> None:
        self._triad_group = _delete(self._triad_group)
        _refresh()

    def clear(self) -> None:
        self.clear_point()
        self.clear_triad()


# ---------------------------------------------------------------------- helpers
def _solid(rgb) -> adsk.fusion.CustomGraphicsSolidColorEffect:
    return adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(rgb[0], rgb[1], rgb[2], 255))


def _add_label(group, text: str, anchor: adsk.core.Point3D, rgb, size_cm: float) -> None:
    """Camera-facing text placed at ``anchor`` (model-space size, so it zooms with the part).

    View scaling is deliberately not used: combined with bill-boarding it moves
    the text away from its anchor by an amount that depends on the anchor
    position. Model-space text stays exactly where it is put.
    """
    transform = adsk.core.Matrix3D.create()
    transform.translation = adsk.core.Vector3D.create(anchor.x + size_cm * 0.8, anchor.y + size_cm * 0.4, anchor.z)
    label = group.addText(text, 'Arial', size_cm, transform)
    label.color = _solid(rgb)
    label.billBoarding = adsk.fusion.CustomGraphicsBillBoard.create(anchor)
    label.depthPriority = 1


def _delete(group):
    if group is not None:
        try:
            group.deleteMe()
        except Exception:
            pass
    return None


def _refresh() -> None:
    """Custom graphics only appear after the viewport repaints."""
    try:
        adsk.core.Application.get().activeViewport.refresh()
    except Exception:
        pass
