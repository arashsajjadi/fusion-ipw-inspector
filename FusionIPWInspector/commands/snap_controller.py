"""Bridges Fusion's hover/click events to the pure-Python snapping core.

* Builds the ``MeshIndex`` of the loaded stock in a background thread (the
  index only reads the STL file, never the Fusion API) and reports readiness
  through a custom event so the dialog can update its status line.
* Converts a hover hit (world, cm) into the mesh frame (mm), derives the
  search radius from a screen-space tolerance in pixels, runs ``snap`` and the
  hysteresis tracker, and hands back the candidate in world coordinates.
* Caches one index per stock fingerprint so reopening the dialog is instant.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, Optional, Tuple

import adsk.core

from ..core.mesh_index import MeshIndex
from ..core import snap as snapcore
from ..core.transform import SetupFrame
from ..diagnostics import log
from ..utils.fusion_units import API_CM_TO_MM, api_point_to_mm, mm_to_api

SNAP_READY_EVENT_ID = 'FusionIPWInspector_SnapReady'
TOLERANCE_PX = 12.0           # screen-space snap tolerance (like CAD object snapping)
MIN_RADIUS_MM = 0.02
MAX_RADIUS_MM = 5.0


class IndexCache:
    """Indexes keyed by stock fingerprint; built once, reused across dialog sessions."""

    def __init__(self) -> None:
        self._indexes: Dict[str, MeshIndex] = {}
        self._building: Dict[str, threading.Thread] = {}
        self.build_seconds: Dict[str, float] = {}

    def get(self, fingerprint: str) -> Optional[MeshIndex]:
        return self._indexes.get(fingerprint)

    def is_building(self, fingerprint: str) -> bool:
        t = self._building.get(fingerprint)
        return t is not None and t.is_alive()

    def build_async(self, fingerprint: str, path: str, scale: float, on_done) -> None:
        if fingerprint in self._indexes or self.is_building(fingerprint):
            return

        def work():
            t0 = time.perf_counter()
            try:
                index = MeshIndex.from_stl(path, scale)
                self._indexes[fingerprint] = index
                self.build_seconds[fingerprint] = time.perf_counter() - t0
                log.info('snap index built: %s in %.2f s' % (index.stats(), self.build_seconds[fingerprint]))
            except Exception as exc:
                log.error('snap index build failed', exc)
            finally:
                try:
                    on_done(fingerprint)
                except Exception:
                    pass

        thread = threading.Thread(target=work, daemon=True, name='ipw-snap-index')
        self._building[fingerprint] = thread
        thread.start()

    def clear(self) -> None:
        self._indexes.clear()


class Preview:
    """What the viewport should show for the current hover."""

    def __init__(self, candidate: snapcore.Candidate, world_mm: Tuple[float, float, float],
                 direction_world: Optional[Tuple[float, float, float]], radius_mm: float, cycle_note: str) -> None:
        self.candidate = candidate
        self.world_mm = world_mm
        self.direction_world = direction_world
        self.radius_mm = radius_mm
        self.cycle_note = cycle_note


class SnapController:
    def __init__(self, app: adsk.core.Application, cache: IndexCache) -> None:
        self.app = app
        self.cache = cache
        self.fingerprint = ''
        self.frame: Optional[SetupFrame] = None
        self.scale = 1.0
        self.index: Optional[MeshIndex] = None
        self.tracker = snapcore.SnapTracker()
        self.preview: Optional[Preview] = None
        self.last_query_ms = 0.0
        self.query_times = []

    # ------------------------------------------------------------ set-up
    def attach(self, fingerprint: str, path: str, unit_scale_mm: float, frame: SetupFrame) -> None:
        """Point the controller at the loaded stock; builds the index if needed."""
        self.fingerprint = fingerprint
        self.frame = frame
        self.scale = unit_scale_mm
        self.tracker.reset()
        self.preview = None
        self.index = self.cache.get(fingerprint)
        if self.index is None:
            self.cache.build_async(fingerprint, path, unit_scale_mm, self._notify_ready)

    def detach(self) -> None:
        self.index = None
        self.frame = None
        self.fingerprint = ''
        self.tracker.reset()
        self.preview = None

    def _notify_ready(self, fingerprint: str) -> None:
        try:
            self.app.fireCustomEvent(SNAP_READY_EVENT_ID, fingerprint)
        except Exception:
            pass

    def refresh_ready(self) -> bool:
        """Called on the main thread after the ready event; True once the index is usable."""
        if self.index is None and self.fingerprint:
            self.index = self.cache.get(self.fingerprint)
        return self.index is not None

    @property
    def state(self) -> str:
        if self.index is not None:
            return 'ready'
        if self.fingerprint and self.cache.is_building(self.fingerprint):
            return 'preparing'
        return 'none'

    # ------------------------------------------------------------- hover
    def hover(self, world_point: adsk.core.Point3D) -> Optional[Preview]:
        """Update the preview for a hover hit (Fusion world point, cm)."""
        if self.index is None or self.frame is None:
            return None
        t0 = time.perf_counter()
        hit_world_mm = api_point_to_mm(world_point)
        hit = self.frame.world_to_setup(hit_world_mm)
        radius = self.radius_mm(world_point)
        result = snapcore.snap(self.index, hit, radius)
        candidate = self.tracker.update(result)
        self.preview = self._preview_for(candidate, radius)
        self.last_query_ms = (time.perf_counter() - t0) * 1000.0
        self.query_times.append(self.last_query_ms)
        if len(self.query_times) > 500:
            del self.query_times[:250]
        return self.preview

    def cycle(self) -> Optional[Preview]:
        if self.tracker.result is None:
            return None
        candidate = self.tracker.cycle()
        if candidate is None:
            return None
        self.preview = self._preview_for(candidate, self.tracker.result.radius)
        return self.preview

    def end_hover(self) -> None:
        self.preview = None
        self.tracker.reset()

    def commit(self, world_point: adsk.core.Point3D) -> Tuple[snapcore.Candidate, Tuple[float, float, float]]:
        """The candidate to use for a click at ``world_point`` (falls back to a fresh snap)."""
        if self.index is None or self.frame is None:
            hit_world_mm = api_point_to_mm(world_point)
            return snapcore.Candidate(snapcore.RAW, hit_world_mm, label='POINT'), hit_world_mm
        hit_world_mm = api_point_to_mm(world_point)
        hit = self.frame.world_to_setup(hit_world_mm)
        radius = self.radius_mm(world_point)
        current = self.tracker.current
        if current is not None and _dist(current.point, hit) <= radius * snapcore.REACH_FACTOR + 1e-9:
            candidate = current
        else:
            candidate = self.tracker.update(snapcore.snap(self.index, hit, radius))
        return candidate, self.frame.setup_to_world(candidate.point)

    # ----------------------------------------------------------- helpers
    def _preview_for(self, candidate: snapcore.Candidate, radius: float) -> Preview:
        world = self.frame.setup_to_world(candidate.point)
        direction = None
        if candidate.direction is not None:
            direction = self.frame.setup_vector_to_world(candidate.direction)
        note = ''
        if self.tracker.result is not None and len(self.tracker.result.candidates) > 1:
            note = '%d/%d' % (self.tracker.cycle_index + 1, len(self.tracker.result.candidates))
        return Preview(candidate, world, direction, radius, note)

    def radius_mm(self, world_point: adsk.core.Point3D) -> float:
        """Screen-space tolerance converted to millimetres at the depth of ``world_point``."""
        try:
            viewport = self.app.activeViewport
            camera = viewport.camera
            eye, target, up = camera.eye, camera.target, camera.upVector
            view_dir = adsk.core.Vector3D.create(target.x - eye.x, target.y - eye.y, target.z - eye.z)
            right = view_dir.crossProduct(up)
            if right.length < 1e-9:
                right = adsk.core.Vector3D.create(1, 0, 0)
            right.normalize()
            p0 = viewport.modelToViewSpace(world_point)
            step_cm = 0.1   # 1 mm
            q = adsk.core.Point3D.create(world_point.x + right.x * step_cm, world_point.y + right.y * step_cm,
                                         world_point.z + right.z * step_cm)
            p1 = viewport.modelToViewSpace(q)
            px_per_mm = math.hypot(p1.x - p0.x, p1.y - p0.y)
            if px_per_mm <= 1e-9:
                return 1.0
            return max(MIN_RADIUS_MM, min(MAX_RADIUS_MM, TOLERANCE_PX / px_per_mm))
        except Exception as exc:
            log.error('screen-space radius failed', exc)
            return 1.0

    def diagnostics(self) -> str:
        if self.index is None:
            return 'snap index: %s' % self.state
        st = self.index.stats()
        times = sorted(self.query_times)
        med = times[len(times) // 2] if times else 0.0
        p95 = times[int(0.95 * (len(times) - 1))] if times else 0.0
        return ('snap index: %d triangles, %d cells of %.2f mm, built in %.2f s; hover %.1f ms median, %.1f ms p95 (%d samples)'
                % (st['triangles'], st['cells'], st['cell_size_mm'], self.cache.build_seconds.get(self.fingerprint, 0.0),
                   med, p95, len(times)))


def _dist(a, b) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)
