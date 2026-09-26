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
import time
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
PREVIEW_GROUP_ID = 'FusionIPWInspector.preview'   # the persistent hover marker (moved, never recreated per hover)
UNIT_CM = 0.1                               # marker geometry is drawn 1 mm big and scaled by the group transform
REFRESH_HZ = 60.0                           # upper bound for viewport repaints caused by hover previews


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
        # Hover preview: one marker group per feature kind (created on first use, then only
        # moved/scaled through its transform) plus one label group; both outlive redraws.
        self._pv_groups: dict = {}
        self._pv_label_group = None
        self._pv_label = None
        self._pv_label_text = ''
        self._pv_visible_kind = None
        self.refresh_on_preview = True
        self.last_refresh_ms = 0.0
        # Fusion does not repaint custom-graphics changes by itself, so every preview move needs a
        # viewport refresh (about 5 ms on this scene). Refreshes are limited to REFRESH_HZ; a move
        # inside the same frame only schedules one deferred refresh (through ``schedule_refresh``).
        self.schedule_refresh = None
        self._last_refresh_at = 0.0
        self._refresh_pending = False
        # Sizes follow the part so they look the same on a watch case and a fixture plate.
        self.label_size_cm = max(0.08, min(1.0, model_size_mm * 0.02 / 10.0))
        self.triad_length_mm = max(3.0, min(60.0, model_size_mm * 0.2))
        self.feature_size_mm = max(0.6, min(8.0, model_size_mm * 0.03))

    def begin(self) -> None:
        """Start a preview redraw: remove the tagged graphics of the previous redraw.

        The hover marker groups carry their own id and survive; Fusion may still
        delete them with the preview, which ``_group_ok`` detects."""
        _delete_tagged(self._groups, GROUP_ID)

    def clear(self) -> None:
        self._pv_groups.clear()
        self._pv_label_group = None
        self._pv_label = None
        self._pv_visible_kind = None
        _delete_tagged(self._groups, GROUP_ID)
        _delete_tagged(self._groups, PREVIEW_GROUP_ID)
        _refresh()

    # ---------------------------------------------------------------- preview
    def show_preview(self, kind: str, world_mm: Sequence[float], direction_world: Optional[Sequence[float]],
                     label: str, size_mm: Optional[float] = None) -> None:
        """Move the persistent hover marker (called from the hover event, never from executePreview).

        Nothing is deleted or recreated per hover: the marker group of ``kind``
        gets a new transform (position, orientation, size), the label text is
        updated only when it changes, and other kinds are hidden."""
        size = size_mm or self.feature_size_mm
        try:
            group = self._pv_groups.get(kind)
            if not _group_ok(group):
                group = self._new_group(PREVIEW_GROUP_ID)
                self._draw_feature(group, kind, (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), 1.0, PREVIEW, ICON_PREVIEW)
                self._pv_groups[kind] = group
            group.transform = _placement(world_mm, direction_world, size)
            if self._pv_visible_kind != kind:
                for k, g in self._pv_groups.items():
                    if k != kind and _group_ok(g) and g.isVisible:
                        g.isVisible = False
                group.isVisible = True
                self._pv_visible_kind = kind
            elif not group.isVisible:
                group.isVisible = True
            self._place_label(label, world_mm, PREVIEW)
            self.last_refresh_ms = 0.0
            if self.refresh_on_preview:
                now = time.perf_counter()
                if now - self._last_refresh_at >= 1.0 / REFRESH_HZ or self.schedule_refresh is None:
                    _refresh()
                    self._last_refresh_at = time.perf_counter()
                    self.last_refresh_ms = (self._last_refresh_at - now) * 1000.0
                elif not self._refresh_pending:
                    self._refresh_pending = True
                    self.schedule_refresh()
        except Exception as exc:
            log.error('could not draw snap preview', exc)

    def deferred_refresh(self) -> None:
        """Runs from the scheduled event: repaint once for all moves that arrived in the same frame."""
        self._refresh_pending = False
        _refresh()
        self._last_refresh_at = time.perf_counter()

    def clear_preview(self, refresh: bool = True) -> None:
        changed = False
        for g in self._pv_groups.values():
            if _group_ok(g) and g.isVisible:
                g.isVisible = False
                changed = True
        if _group_ok(self._pv_label_group) and self._pv_label_group.isVisible:
            self._pv_label_group.isVisible = False
            changed = True
        self._pv_visible_kind = None
        if changed and refresh:
            _refresh()

    def _place_label(self, text: str, world_mm, rgb) -> None:
        anchor = _pt(world_mm)
        group = self._pv_label_group
        if not _group_ok(group) or self._pv_label is None:
            group = self._new_group(PREVIEW_GROUP_ID)
            self._pv_label = _add_label(group, text, anchor, rgb, self.label_size_cm)
            self._pv_label_group = group
            self._pv_label_text = text
        else:
            if text != self._pv_label_text:
                self._pv_label.text = text
                self._pv_label_text = text
            transform = adsk.core.Matrix3D.create()
            transform.translation = adsk.core.Vector3D.create(anchor.x + self.label_size_cm * 0.8,
                                                              anchor.y + self.label_size_cm * 0.4, anchor.z)
            self._pv_label.transform = transform
            self._pv_label.billBoarding = adsk.fusion.CustomGraphicsBillBoard.create(anchor)
        if not group.isVisible:
            group.isVisible = True

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

    def _new_group(self, group_id: str = GROUP_ID):
        group = self._groups.add()
        try:
            group.id = group_id
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


def _add_label(group, text: str, anchor: adsk.core.Point3D, rgb, size_cm: float):
    """Camera-facing text placed next to ``anchor`` (model-space size, so it zooms with the part)."""
    transform = adsk.core.Matrix3D.create()
    transform.translation = adsk.core.Vector3D.create(anchor.x + size_cm * 0.8, anchor.y + size_cm * 0.4, anchor.z)
    label = group.addText(text, 'Arial', size_cm, transform)
    _passive(label)
    label.color = _solid(rgb)
    label.billBoarding = adsk.fusion.CustomGraphicsBillBoard.create(anchor)
    label.depthPriority = 1
    return label


def _delete_tagged(groups, group_id: str = GROUP_ID) -> int:
    """Delete the add-in's groups with ``group_id`` in one collection; returns how many were removed."""
    removed = 0
    try:
        for group in [groups.item(i) for i in range(groups.count)]:
            try:
                if group.isValid and group.id == group_id and group.deleteMe():
                    removed += 1
            except Exception:
                continue
    except Exception as exc:
        log.error('graphics cleanup failed', exc)
    return removed


def _group_ok(group) -> bool:
    try:
        return group is not None and group.isValid
    except Exception:
        return False


def _placement(world_mm, direction_world, size_mm: float) -> adsk.core.Matrix3D:
    """Transform that moves the unit marker to ``world_mm``, scales it to ``size_mm`` and, for
    edges, aligns its +X with the edge direction."""
    s = float(size_mm)
    if direction_world is not None:
        u = _unit(direction_world)
        ref = (0.0, 0.0, 1.0) if abs(u[2]) < 0.9 else (1.0, 0.0, 0.0)
        v = _unit((u[1] * ref[2] - u[2] * ref[1], u[2] * ref[0] - u[0] * ref[2], u[0] * ref[1] - u[1] * ref[0]))
        w = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    else:
        u, v, w = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    m = adsk.core.Matrix3D.create()
    m.setWithCoordinateSystem(_pt(world_mm),
                              adsk.core.Vector3D.create(u[0] * s, u[1] * s, u[2] * s),
                              adsk.core.Vector3D.create(v[0] * s, v[1] * s, v[2] * s),
                              adsk.core.Vector3D.create(w[0] * s, w[1] * s, w[2] * s))
    return m


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
                removed += _delete_tagged(groups, GROUP_ID)
                removed += _delete_tagged(groups, PREVIEW_GROUP_ID)
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
