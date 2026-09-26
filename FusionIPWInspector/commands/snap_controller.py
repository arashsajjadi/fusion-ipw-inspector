"""Bridges Fusion's mouse events to the pure-Python snapping core.

Per stock (keyed by its fingerprint), once:

* the ``MeshIndex`` (analysis mesh: the full-resolution exported STL, spatially
  indexed) and the ``FeatureGraph`` (patches, edges, corners) are built in a
  background thread, or loaded from the cache file next to the STL; readiness
  is reported through a custom event so the dialog can update its status line.

Per hover (main thread, no Fusion document access):

1. the cursor's viewport position becomes a ray (camera eye/target, view space
   to model space), expressed in the mesh frame through the setup WCS;
2. the ray is cast against the analysis mesh (grid traversal, first hit);
3. the screen tolerance (12 px) is converted to millimetres at the hit depth;
4. the feature graph is asked for the corners, edges and patches within reach
   (local reconstruction is the fallback while the graph is still building);
5. the magnetic tracker picks the candidate to preview (acquire / retain);
6. the result is handed back in world coordinates, with a ``changed`` flag so
   graphics are only touched when something moved.

Every stage is timed into ``HoverProfile`` and summarised in the log on close.
"""
from __future__ import annotations

import math
import os
import pickle
import threading
import time
from typing import Dict, List, Optional, Tuple

import adsk.core

from ..core.features import FeatureGraph
from ..core.mesh_index import MeshIndex
from ..core import snap as snapcore
from ..core.transform import SetupFrame
from ..diagnostics import log
from ..utils.fusion_units import API_CM_TO_MM, api_point_to_mm

SNAP_READY_EVENT_ID = 'FusionIPWInspector_SnapReady'
TOLERANCE_PX = 12.0           # screen-space snap tolerance (like CAD object snapping)
MIN_RADIUS_MM = 0.02
MAX_RADIUS_MM = 5.0
RETAIN_FACTOR = 2.0           # an acquired feature is held up to this many tolerances away
CACHE_VERSION = 4             # bump when the index or feature layout changes
CACHE_SUFFIX = '.snapcache'


class HoverProfile:
    """Per-stage timings of the hover pipeline (kept small; summarised in the log on close)."""

    STAGES = ('interval', 'ray', 'cast', 'radius', 'query', 'track', 'graphics', 'refresh', 'total')

    def __init__(self) -> None:
        self.samples: Dict[str, list] = {k: [] for k in self.STAGES}
        self.last_event = 0.0
        self.mouse_moves = 0
        self.coalesced = 0
        self.misses = 0

    def add(self, stage: str, ms: float) -> None:
        lst = self.samples[stage]
        lst.append(ms)
        if len(lst) > 4000:
            del lst[:2000]

    def event(self) -> None:
        now = time.perf_counter()
        if self.last_event:
            self.add('interval', (now - self.last_event) * 1000.0)
        self.last_event = now

    @staticmethod
    def _stats(values) -> str:
        if not values:
            return 'n=0'
        v = sorted(values)
        med = v[len(v) // 2]
        p95 = v[int(0.95 * (len(v) - 1))]
        return 'n=%d med %.2f p95 %.2f max %.2f ms' % (len(v), med, p95, v[-1])

    def summary(self) -> str:
        parts = ['%s: %s' % (k, self._stats(self.samples[k])) for k in self.STAGES]
        parts.append('mouseMove events %d, coalesced %d, misses %d' % (self.mouse_moves, self.coalesced, self.misses))
        return 'hover profile | ' + ' | '.join(parts)


class IndexCache:
    """Analysis data keyed by stock fingerprint; built once, reused across dialog sessions.

    ``build_async`` loads the cache file next to the STL when it matches the
    fingerprint, otherwise builds the index (reported first, so picking and
    local snapping work early) and then the feature graph, and writes the cache
    file for the next Fusion session.
    """

    def __init__(self) -> None:
        self._indexes: Dict[str, MeshIndex] = {}
        self._graphs: Dict[str, FeatureGraph] = {}
        self._building: Dict[str, threading.Thread] = {}
        self.timings: Dict[str, Dict[str, float]] = {}

    def get(self, fingerprint: str) -> Optional[MeshIndex]:
        return self._indexes.get(fingerprint)

    def get_graph(self, fingerprint: str) -> Optional[FeatureGraph]:
        return self._graphs.get(fingerprint)

    def is_building(self, fingerprint: str) -> bool:
        t = self._building.get(fingerprint)
        return t is not None and t.is_alive()

    def build_async(self, fingerprint: str, path: str, scale: float, on_done) -> None:
        if fingerprint in self._graphs or self.is_building(fingerprint):
            return

        def notify(stage: str) -> None:
            try:
                on_done(fingerprint, stage)
            except Exception:
                pass

        def work():
            timing: Dict[str, float] = {}
            self.timings[fingerprint] = timing
            try:
                cache_path = path + CACHE_SUFFIX
                loaded = self._load_cache(cache_path, fingerprint, timing)
                if loaded is not None:
                    index, graph = loaded
                    self._indexes[fingerprint] = index
                    self._graphs[fingerprint] = graph
                    log.info('snap cache loaded in %.2f s: %s' % (timing['cache_load'], graph.stats()))
                    notify('index')
                    notify('features')
                    return
                t0 = time.perf_counter()
                index = self._indexes.get(fingerprint) or MeshIndex.from_stl(path, scale)
                timing['index'] = time.perf_counter() - t0
                self._indexes[fingerprint] = index
                log.info('snap index built: %s in %.2f s' % (index.stats(), timing['index']))
                notify('index')
                t0 = time.perf_counter()
                graph = FeatureGraph.build(index)
                timing['features'] = time.perf_counter() - t0
                self._graphs[fingerprint] = graph
                log.info('feature graph built: %s' % graph.stats())
                notify('features')
                t0 = time.perf_counter()
                self._save_cache(cache_path, fingerprint, index, graph)
                timing['cache_save'] = time.perf_counter() - t0
            except Exception as exc:
                log.error('snap preparation failed', exc)
                notify('failed')

        thread = threading.Thread(target=work, daemon=True, name='ipw-snap-prepare')
        self._building[fingerprint] = thread
        thread.start()

    @staticmethod
    def _load_cache(cache_path: str, fingerprint: str, timing: Dict[str, float]):
        if not os.path.isfile(cache_path):
            log.info('snap cache: no file yet (%s)' % os.path.basename(cache_path))
            return None
        t0 = time.perf_counter()
        try:
            with open(cache_path, 'rb') as fh:
                header = pickle.load(fh)
                if header.get('version') != CACHE_VERSION or header.get('fingerprint') != fingerprint:
                    log.info('snap cache: stale (file %s/%s, stock %s/%s); rebuilding' % (
                        header.get('version'), header.get('fingerprint'), CACHE_VERSION, fingerprint))
                    return None
                index_state, graph_state = pickle.load(fh)
            index = MeshIndex.from_state(index_state)
            graph = FeatureGraph.from_state(graph_state, index)
        except Exception as exc:
            log.info('snap cache unusable (%s); rebuilding' % exc)
            return None
        timing['cache_load'] = time.perf_counter() - t0
        return index, graph

    @staticmethod
    def _save_cache(cache_path: str, fingerprint: str, index: MeshIndex, graph: FeatureGraph) -> None:
        try:
            tmp = cache_path + '.tmp'
            with open(tmp, 'wb') as fh:
                pickle.dump({'version': CACHE_VERSION, 'fingerprint': fingerprint}, fh, protocol=pickle.HIGHEST_PROTOCOL)
                pickle.dump((index.to_state(), graph.to_state()), fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cache_path)
        except Exception as exc:
            log.error('could not write the snap cache', exc)

    def clear(self) -> None:
        self._indexes.clear()
        self._graphs.clear()


class Preview:
    """What the viewport should show for the current hover."""

    __slots__ = ('candidate', 'world_mm', 'direction_world', 'radius_mm', 'cycle_note', 'changed', 'signature')

    def __init__(self, candidate: snapcore.Candidate, world_mm: Tuple[float, float, float],
                 direction_world: Optional[Tuple[float, float, float]], radius_mm: float, cycle_note: str) -> None:
        self.candidate = candidate
        self.world_mm = world_mm
        self.direction_world = direction_world
        self.radius_mm = radius_mm
        self.cycle_note = cycle_note
        self.changed = True
        self.signature = (candidate.key, candidate.kind, cycle_note,
                          round(world_mm[0], 4), round(world_mm[1], 4), round(world_mm[2], 4), round(radius_mm, 3))


class SnapController:
    def __init__(self, app: adsk.core.Application, cache: IndexCache) -> None:
        self.app = app
        self.cache = cache
        self.fingerprint = ''
        self.frame: Optional[SetupFrame] = None
        self.scale = 1.0
        self.index: Optional[MeshIndex] = None
        self.graph: Optional[FeatureGraph] = None
        self.tracker = snapcore.MagneticTracker(RETAIN_FACTOR)
        self.preview: Optional[Preview] = None
        self.last_hit: Optional[Tuple[Tuple[float, float, float], int, float]] = None   # (setup mm, triangle, radius)
        self.query_times: List[float] = []
        self.profile = HoverProfile()
        self._last_view: Optional[Tuple[float, float]] = None
        self._last_signature = None

    # ------------------------------------------------------------ set-up
    def attach(self, fingerprint: str, path: str, unit_scale_mm: float, frame: SetupFrame) -> None:
        """Point the controller at the loaded stock; builds or loads the analysis data if needed."""
        self.fingerprint = fingerprint
        self.frame = frame
        self.scale = unit_scale_mm
        self.tracker.reset()
        self.preview = None
        self.last_hit = None
        self.index = self.cache.get(fingerprint)
        self.graph = self.cache.get_graph(fingerprint)
        if self.graph is None:
            self.cache.build_async(fingerprint, path, unit_scale_mm, self._notify_ready)

    def detach(self) -> None:
        self.index = None
        self.graph = None
        self.frame = None
        self.fingerprint = ''
        self.tracker.reset()
        self.preview = None
        self.last_hit = None

    def _notify_ready(self, fingerprint: str, stage: str) -> None:
        try:
            self.app.fireCustomEvent(SNAP_READY_EVENT_ID, '%s|%s' % (fingerprint, stage))
        except Exception:
            pass

    def refresh_ready(self) -> bool:
        """Called on the main thread after a ready event; True once the index is usable."""
        if self.fingerprint:
            if self.index is None:
                self.index = self.cache.get(self.fingerprint)
            if self.graph is None:
                self.graph = self.cache.get_graph(self.fingerprint)
        return self.index is not None

    @property
    def state(self) -> str:
        if self.graph is not None or self.index is not None:
            return 'ready'          # picking and local snapping work; features arrive silently
        if self.fingerprint and self.cache.is_building(self.fingerprint):
            return 'preparing'
        return 'none'

    @property
    def features_ready(self) -> bool:
        return self.graph is not None

    # ------------------------------------------------------------- hover
    def hover_view(self, viewport: adsk.core.Viewport, x: float, y: float) -> Optional[Preview]:
        """Hover at viewport pixel (x, y): ray cast on the analysis mesh, then snap.

        Returns None when the ray misses the stock (the caller hides the preview).
        Identical positions are coalesced; the newest position always wins because
        events are handled synchronously and each one costs about a millisecond.
        """
        if self.index is None or self.frame is None:
            return None
        prof = self.profile
        prof.mouse_moves += 1
        if self._last_view == (x, y):
            prof.coalesced += 1
            return self.preview
        self._last_view = (x, y)
        prof.event()
        t0 = time.perf_counter()
        ray = self._ray(viewport, x, y)
        t1 = time.perf_counter()
        prof.add('ray', (t1 - t0) * 1000.0)
        if ray is None:
            return None
        origin, direction = ray
        hit = self.index.raycast(origin, direction)
        t2 = time.perf_counter()
        prof.add('cast', (t2 - t1) * 1000.0)
        if hit is None:
            prof.misses += 1
            return None
        t_hit, tri, point = hit
        world_mm = self.frame.setup_to_world(point)
        hit_world = adsk.core.Point3D.create(world_mm[0] / API_CM_TO_MM, world_mm[1] / API_CM_TO_MM, world_mm[2] / API_CM_TO_MM)
        radius = self.radius_mm(hit_world, viewport)
        t3 = time.perf_counter()
        prof.add('radius', (t3 - t2) * 1000.0)
        return self._snap_at(point, tri, radius, t3)

    def hover(self, world_point: adsk.core.Point3D) -> Optional[Preview]:
        """Hover from a Fusion selection hit (legacy path; the hit is already on the stock)."""
        if self.index is None or self.frame is None:
            return None
        self.profile.event()
        t0 = time.perf_counter()
        hit = self.frame.world_to_setup(api_point_to_mm(world_point))
        radius = self.radius_mm(world_point, self.app.activeViewport)
        t1 = time.perf_counter()
        self.profile.add('radius', (t1 - t0) * 1000.0)
        return self._snap_at(hit, None, radius, t1)

    def _snap_at(self, hit, tri: Optional[int], radius: float, t_start: float) -> Preview:
        prof = self.profile
        cands = self._candidates(hit, tri, radius)
        t2 = time.perf_counter()
        prof.add('query', (t2 - t_start) * 1000.0)
        self.query_times.append((t2 - t_start) * 1000.0)
        if len(self.query_times) > 1000:
            del self.query_times[:500]
        candidate = self.tracker.update(cands, radius)
        self.last_hit = (hit, tri, radius)
        preview = self._preview_for(candidate, radius)
        t3 = time.perf_counter()
        prof.add('track', (t3 - t2) * 1000.0)
        return preview

    def _candidates(self, hit, tri: Optional[int], radius: float) -> List[snapcore.Candidate]:
        reach = radius * RETAIN_FACTOR
        if self.graph is not None:
            found = self.graph.query(hit, reach, tri)
            cands = []
            for f in found:
                kind = f['kind']
                label = {'corner': 'CORNER', 'edge': 'EDGE', 'surface': 'SURFACE'}[kind]
                cands.append(snapcore.Candidate(kind, f['point'], [], f['rms'], f['conditioning'], f['distance'],
                                                direction=f['direction'], label=label, key=f['key']))
            cands.append(snapcore.Candidate(snapcore.RAW, hit, [], 0.0, 1.0, 0.0, label='POINT', key=('raw',)))
            cands.sort(key=lambda c: (-c.priority, c.distance_from_hit))
            return cands
        result = snapcore.snap(self.index, hit, radius)
        return snapcore.keyed(result)

    def cycle(self) -> Optional[Preview]:
        candidate = self.tracker.cycle()
        if candidate is None:
            return None
        return self._preview_for(candidate, self.tracker.radius)

    def end_hover(self) -> None:
        self.preview = None
        self.last_hit = None
        self._last_view = None
        self._last_signature = None
        self.tracker.reset()

    def commit_current(self) -> Optional[Tuple[snapcore.Candidate, Tuple[float, float, float]]]:
        """The previewed candidate for a click at the last hovered position."""
        cur = self.tracker.current
        if cur is None or self.frame is None:
            return None
        return cur, self.frame.setup_to_world(cur.point)

    def commit(self, world_point: adsk.core.Point3D) -> Tuple[snapcore.Candidate, Tuple[float, float, float]]:
        """The candidate for a click at ``world_point`` (selection path; falls back to a fresh snap)."""
        hit_world_mm = api_point_to_mm(world_point)
        if self.index is None or self.frame is None:
            return snapcore.Candidate(snapcore.RAW, hit_world_mm, label='POINT', key=('raw',)), hit_world_mm
        hit = self.frame.world_to_setup(hit_world_mm)
        radius = self.radius_mm(world_point, self.app.activeViewport)
        current = self.tracker.current
        if current is not None and _dist(current.point, hit) <= radius * RETAIN_FACTOR + 1e-9:
            candidate = current
        else:
            candidate = self.tracker.update(self._candidates(hit, None, radius), radius)
        return candidate, self.frame.setup_to_world(candidate.point)

    # ----------------------------------------------------------- helpers
    def _preview_for(self, candidate: snapcore.Candidate, radius: float) -> Preview:
        world = self.frame.setup_to_world(candidate.point)
        direction = None
        if candidate.direction is not None:
            direction = self.frame.setup_vector_to_world(candidate.direction)
        note = ''
        n = len(self.tracker.candidates)
        if n > 1:
            note = '%d/%d' % (self.tracker.cycle_index + 1, n)
        pv = Preview(candidate, world, direction, radius, note)
        pv.changed = pv.signature != self._last_signature
        self._last_signature = pv.signature
        self.preview = pv
        return pv

    def _ray(self, viewport: adsk.core.Viewport, x: float, y: float):
        """Ray through viewport pixel (x, y) as (origin, direction) in the mesh frame (mm)."""
        try:
            p = viewport.viewToModelSpace(adsk.core.Point2D.create(x, y))
            camera = viewport.camera
            eye, target = camera.eye, camera.target
            if camera.cameraType == adsk.core.CameraTypes.PerspectiveCameraType:
                origin = (eye.x, eye.y, eye.z)
                d = (p.x - eye.x, p.y - eye.y, p.z - eye.z)
            else:
                d = (target.x - eye.x, target.y - eye.y, target.z - eye.z)
                ln = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) or 1.0
                d = (d[0] / ln, d[1] / ln, d[2] / ln)
                back = 1000.0     # cm: start well outside any stock
                origin = (p.x - d[0] * back, p.y - d[1] * back, p.z - d[2] * back)
            origin_mm = self.frame.world_to_setup((origin[0] * API_CM_TO_MM, origin[1] * API_CM_TO_MM, origin[2] * API_CM_TO_MM))
            return origin_mm, self.frame.world_vector_to_setup(d)
        except Exception as exc:
            log.error('ray construction failed', exc)
            return None

    def radius_mm(self, world_point: adsk.core.Point3D, viewport: Optional[adsk.core.Viewport] = None) -> float:
        """Screen-space tolerance converted to millimetres at the depth of ``world_point``."""
        try:
            viewport = viewport or self.app.activeViewport
            camera = viewport.camera
            eye, target, up = camera.eye, camera.target, camera.upVector
            view_dir = adsk.core.Vector3D.create(target.x - eye.x, target.y - eye.y, target.z - eye.z)
            right = view_dir.crossProduct(up)
            if right.length < 1e-9:
                right = adsk.core.Vector3D.create(1, 0, 0)
            right.normalize()
            p0 = viewport.modelToViewSpace(world_point)
            step_cm = 1.0   # 10 mm: integer pixel coordinates would quantise a 1 mm step too coarsely
            q = adsk.core.Point3D.create(world_point.x + right.x * step_cm, world_point.y + right.y * step_cm,
                                         world_point.z + right.z * step_cm)
            p1 = viewport.modelToViewSpace(q)
            px_per_mm = math.hypot(p1.x - p0.x, p1.y - p0.y) / (step_cm * API_CM_TO_MM)
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
        timing = self.cache.timings.get(self.fingerprint, {})
        built = ('cache %.2f s' % timing['cache_load']) if 'cache_load' in timing else \
                ('index %.2f s, features %.2f s' % (timing.get('index', 0.0), timing.get('features', 0.0)))
        feat = ''
        if self.graph is not None:
            gs = self.graph.stats()
            feat = '; features: %d patches, %d edges, %d corners' % (gs['patches'], gs['edges'], gs['corners'])
        return ('snap: %d triangles, %d cells of %.2f mm, %s%s; hover query %.2f ms median, %.2f ms p95 (%d samples)'
                % (st['triangles'], st['cells'], st['cell_size_mm'], built, feat, med, p95, len(times)))


def _dist(a, b) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)
