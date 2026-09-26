"""Viewport feedback: snap preview, picked-point marker and the WCS triad.

Everything is drawn with Fusion custom graphics: display-only, never saved,
gone when the command closes. Feature kinds are told apart by shape *and*
label, never by colour alone:

    CORNER   crosshair icon + small 3D cross
    EDGE     short line along the reconstructed edge + a bar icon
    SURFACE  small square drawn in the fitted plane
    POINT    small dot (raw mesh hit)
"""
from __future__ import annotations

import math
import os
from typing import Optional, Sequence

import adsk.cam
import adsk.core
import adsk.fusion

from ..core.transform import SetupFrame
from ..diagnostics import log
from ..utils.fusion_units import mm_to_api

ACCENT = (255, 122, 0)            # picked point
PREVIEW = (0, 190, 255)           # hover preview
AXIS_COLORS = {'X': (220, 40, 40), 'Y': (40, 170, 40), 'Z': (40, 90, 230)}
RESOURCES = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources', 'marker')
ICON_CROSSHAIR = os.path.join(RESOURCES, 'crosshair.png')
ICON_PREVIEW = os.path.join(RESOURCES, 'preview.png')
GROUP_ID = 'FusionIPWInspector.graphics'   # tag on every group so leftovers can be found and removed


class Markers:
    """Draws the add-in's viewport graphics for one dialog session.

    Fusion deletes graphics created during ``executePreview`` before the next
    preview runs, so nothing is cached across redraws: ``begin()`` removes any
    tagged group that still exists, then the ``show_*`` calls draw the current
    state from scratch. Every group carries ``GROUP_ID`` so leftovers can be
    found and removed later by :func:`sweep_orphans`.
    """

    def __init__(self, graphics_groups: adsk.fusion.CustomGraphicsGroups, model_size_mm: float = 50.0) -> None:
        self._groups = graphics_groups
        self._preview_group = None      # drawn outside executePreview, so it is tracked explicitly
        # Sizes follow the part so they look the same on a watch case and a fixture plate.
        self.label_size_cm = max(0.08, min(1.0, model_size_mm * 0.02 / 10.0))
        self.triad_length_mm = max(3.0, min(60.0, model_size_mm * 0.2))
        self.feature_size_mm = max(0.6, min(8.0, model_size_mm * 0.03))

    def begin(self) -> None:
        """Start a preview redraw: remove whatever tagged graphics still exist."""
        self._preview_group = None
        _delete_tagged(self._groups)

    def clear(self) -> None:
        self._preview_group = None
        _delete_tagged(self._groups)
        _refresh()

    # ---------------------------------------------------------------- preview
    def show_preview(self, kind: str, world_mm: Sequence[float], direction_world: Optional[Sequence[float]],
                     label: str, size_mm: Optional[float] = None) -> None:
        """Draw the hover preview immediately (called from the hover event, not from executePreview).

        Requesting an executePreview from inside a hover event cancels the click
        that follows it, so the preview is drawn and replaced directly here.
        """
        self.clear_preview(refresh=False)
        try:
            group = self._new_group()
            size = size_mm or self.feature_size_mm
            self._draw_feature(group, kind, world_mm, direction_world, size, PREVIEW, ICON_PREVIEW)
            if label:
                _add_label(group, label, _pt(world_mm), PREVIEW, self.label_size_cm)
            self._preview_group = group
            _refresh()
        except Exception as exc:
            log.error('could not draw snap preview', exc)

    def clear_preview(self, refresh: bool = True) -> None:
        group, self._preview_group = self._preview_group, None
        if group is not None:
            try:
                if group.isValid:
                    group.deleteMe()
            except Exception:
                pass
            if refresh:
                _refresh()

    # ----------------------------------------------------------------- point
    def show_point(self, kind: str, world_mm: Sequence[float], direction_world: Optional[Sequence[float]],
                   label: str = '') -> None:
        try:
            group = self._new_group()
            self._draw_feature(group, kind, world_mm, direction_world, self.feature_size_mm, ACCENT, ICON_CROSSHAIR)
            if label:
                _add_label(group, label, _pt(world_mm), ACCENT, self.label_size_cm)
        except Exception as exc:
            log.error('could not draw point marker', exc)

    # ----------------------------------------------------------------- triad
    def show_triad(self, frame: SetupFrame) -> None:
        try:
            group = self._new_group()
            o = frame.origin
            ox, oy, oz = mm_to_api(o)
            for name, axis in (('X', frame.axis_x), ('Y', frame.axis_y), ('Z', frame.axis_z)):
                tip = tuple(o[i] + axis[i] * self.triad_length_mm for i in range(3))
                tx, ty, tz = mm_to_api(tip)
                line = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create([ox, oy, oz, tx, ty, tz]), [0, 1], False)
                _passive(line)
                line.color = _solid(AXIS_COLORS[name])
                line.weight = 2.0
                line.depthPriority = 1
                _add_label(group, name, adsk.core.Point3D.create(tx, ty, tz), AXIS_COLORS[name], self.label_size_cm)
            dot = group.addPointSet(adsk.fusion.CustomGraphicsCoordinates.create([ox, oy, oz]), [0],
                                    adsk.fusion.CustomGraphicsPointTypes.PointCloudCustomGraphicsPointType, '')
            _passive(dot)
            dot.color = _solid((255, 255, 255))
            dot.depthPriority = 1
        except Exception as exc:
            log.error('could not draw WCS triad', exc)

    def _new_group(self):
        group = self._groups.add()
        try:
            group.id = GROUP_ID
            group.isSelectable = False      # the marker must never sit between the cursor and the stock
        except Exception:
            pass
        return group

    # --------------------------------------------------------------- drawing
    def _draw_feature(self, group, kind: str, world_mm, direction_world, size_mm: float, rgb, icon: str) -> None:
        cx, cy, cz = mm_to_api(world_mm)
        half = size_mm / 10.0
        if kind == 'edge' and direction_world is not None:
            d = _unit(direction_world)
            coords = [cx - d[0] * half * 2, cy - d[1] * half * 2, cz - d[2] * half * 2,
                      cx + d[0] * half * 2, cy + d[1] * half * 2, cz + d[2] * half * 2]
            line = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create(coords), [0, 1], False)
            _passive(line)
            line.color = _solid(rgb)
            line.weight = 3.0
            line.depthPriority = 1
        elif kind == 'surface':
            # a small square in a plane perpendicular to the view is not known here; draw a
            # flat diamond of 3D lines around the point instead (visible from any angle)
            coords = [cx - half, cy, cz, cx, cy + half, cz, cx + half, cy, cz, cx, cy - half, cz,
                      cx, cy, cz - half, cx, cy, cz + half]
            lines = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create(coords), [0, 1, 1, 2, 2, 3, 3, 0, 4, 5], False)
            _passive(lines)
            lines.color = _solid(rgb)
            lines.weight = 1.5
            lines.depthPriority = 1
        elif kind == 'corner':
            coords = [cx - half, cy, cz, cx + half, cy, cz, cx, cy - half, cz, cx, cy + half, cz,
                      cx, cy, cz - half, cx, cy, cz + half]
            lines = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create(coords), [0, 1, 2, 3, 4, 5], False)
            _passive(lines)
            lines.color = _solid(rgb)
            lines.weight = 2.0
            lines.depthPriority = 1
        if os.path.isfile(icon):
            pts = group.addPointSet(adsk.fusion.CustomGraphicsCoordinates.create([cx, cy, cz]), [0],
                                    adsk.fusion.CustomGraphicsPointTypes.UserDefinedCustomGraphicsPointType, icon)
            _passive(pts)
            pts.depthPriority = 1
        else:
            dot = group.addPointSet(adsk.fusion.CustomGraphicsCoordinates.create([cx, cy, cz]), [0],
                                    adsk.fusion.CustomGraphicsPointTypes.PointCloudCustomGraphicsPointType, '')
            _passive(dot)
            dot.color = _solid(rgb)
            dot.depthPriority = 1


# ---------------------------------------------------------------------- helpers
def _pt(world_mm) -> adsk.core.Point3D:
    return adsk.core.Point3D.create(*mm_to_api(world_mm))


def _unit(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _passive(entity):
    """Custom graphics are feedback only: they must not capture clicks meant for the stock."""
    try:
        entity.isSelectable = False
    except Exception:
        pass
    return entity


def _solid(rgb) -> adsk.fusion.CustomGraphicsSolidColorEffect:
    return adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(rgb[0], rgb[1], rgb[2], 255))


def _add_label(group, text: str, anchor: adsk.core.Point3D, rgb, size_cm: float) -> None:
    """Camera-facing text placed next to ``anchor`` (model-space size, so it zooms with the part)."""
    transform = adsk.core.Matrix3D.create()
    transform.translation = adsk.core.Vector3D.create(anchor.x + size_cm * 0.8, anchor.y + size_cm * 0.4, anchor.z)
    label = group.addText(text, 'Arial', size_cm, transform)
    _passive(label)
    label.color = _solid(rgb)
    label.billBoarding = adsk.fusion.CustomGraphicsBillBoard.create(anchor)
    label.depthPriority = 1


def _delete_tagged(groups) -> int:
    """Delete the add-in's groups in one collection; returns how many were removed."""
    removed = 0
    try:
        for group in [groups.item(i) for i in range(groups.count)]:
            try:
                if group.isValid and group.id == GROUP_ID and group.deleteMe():
                    removed += 1
            except Exception:
                continue
    except Exception as exc:
        log.error('graphics cleanup failed', exc)
    return removed


def sweep_orphans(app: adsk.core.Application, doc: Optional[adsk.core.Document] = None) -> int:
    """Delete every graphics group tagged with ``GROUP_ID`` in ``doc`` (or in all open documents).

    Graphics drawn inside a command's preview are tied to that command's
    transaction: deleting them while the command is being destroyed does not
    always stick (Fusion restores the last preview state). The add-in therefore
    runs this sweep after the command has fully terminated, when a session
    starts, and when the add-in stops. Only groups carrying the add-in's own id
    are touched.
    """
    removed = 0
    docs = [doc] if doc is not None else [app.documents.item(i) for i in range(app.documents.count)]
    for d in docs:
        try:
            collections = []
            for product_type, getter in (('CAMProductType', lambda p: adsk.cam.CAM.cast(p).customGraphicsGroups),
                                         ('DesignProductType', lambda p: adsk.fusion.Design.cast(p).rootComponent.customGraphicsGroups)):
                try:
                    product = d.products.itemByProductType(product_type)
                except Exception:
                    product = None          # a document without that product raises instead of returning None
                if product is not None:
                    try:
                        collections.append(getter(product))
                    except Exception:
                        pass
            for groups in collections:
                removed += _delete_tagged(groups)
        except Exception as exc:
            log.error('graphics sweep failed', exc)
    if removed:
        log.info('removed %d leftover graphics group(s)' % removed)
        _refresh()
    return removed


def _refresh() -> None:
    try:
        adsk.core.Application.get().activeViewport.refresh()
    except Exception:
        pass
