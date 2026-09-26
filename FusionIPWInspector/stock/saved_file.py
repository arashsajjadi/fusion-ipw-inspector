"""Fallback provider: a stock file the user saved from Simulation.

Used only when the post-engine export is not possible (for example a setup
without operations). Two ways in:

* ``FolderWatcher`` watches the folder the user last saved to and reports a
  new ``.stl`` the moment Simulation writes it (Windows change notifications,
  no polling).
* ``load`` imports a chosen file; unit and frame are inferred by checking
  that the stock encloses the setup's model, then validated.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from typing import Callable, List, Optional, Sequence

import adsk.fusion

from ..core.stock_file import Box, StockFileError, infer_interpretation, read_stock_file
from ..core.transform import SetupFrame
from ..diagnostics import log
from . import mesh_validation
from .post_export import _box_to_frame, _file_fingerprint
from .provider import SOURCE_NONE, SOURCE_SAVED, StockResult
from .temporary_mesh import TemporaryMesh

STOCK_EXTENSIONS = ('.stl',)


class SavedStockProvider:
    name = 'saved-file'

    def __init__(self, design: adsk.fusion.Design) -> None:
        self.design = design
        self.temporary = TemporaryMesh(design)

    def load(self, path: str, setup_name: str, setup_id: int, model_box_world_mm: Optional[Box],
             candidate_frames: Sequence[SetupFrame]) -> StockResult:
        if not os.path.isfile(path):
            return StockResult(SOURCE_NONE, setup_name, setup_id, error='the file no longer exists: %s' % path)
        try:
            info = read_stock_file(path)
        except StockFileError as exc:
            return StockResult(SOURCE_NONE, setup_name, setup_id, error=str(exc), provider=self.name)
        interp = infer_interpretation(info.bbox, model_box_world_mm, candidate_frames)
        frame = interp.frame
        model_box_frame = _box_to_frame(model_box_world_mm, frame) if model_box_world_mm else None
        report = mesh_validation.validate_stock(info, interp.scale_to_mm, model_box_frame)
        result = StockResult(SOURCE_SAVED, setup_name, setup_id, frame=frame, stock_path=path,
                             unit_scale_mm=interp.scale_to_mm, triangle_count=info.triangle_count,
                             mean_edge_mm=info.mean_edge * interp.scale_to_mm, provider=self.name,
                             is_plain_box=info.triangle_count <= mesh_validation.PLAIN_BOX_TRIANGLES,
                             fingerprint=_file_fingerprint(path))
        result.notes.append('%s read as %s.' % (os.path.basename(path), interp.label))
        result.notes.extend(report.notes)
        if not report.ok:
            result.source = SOURCE_NONE
            result.error = ' '.join(report.problems)
            return result
        if self.temporary.find_current(setup_id, result.fingerprint) is None:
            self.temporary.remove_all()
            self.temporary.import_mesh(path, interp.scale_to_mm, frame, setup_id, result.fingerprint,
                                       body_name='Saved stock')
        result.mesh_body_valid = True
        return result


def newest_stock_file(folder: str, newer_than: float = 0.0) -> Optional[str]:
    """Most recent stock file in ``folder`` written after ``newer_than`` (epoch seconds)."""
    best, best_time = None, newer_than
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(STOCK_EXTENSIONS):
                path = os.path.join(folder, name)
                mtime = os.path.getmtime(path)
                if mtime > best_time:
                    best, best_time = path, mtime
    except OSError:
        return None
    return best


class FolderWatcher:
    """Reports new stock files in one folder using Windows change notifications.

    ``callback`` is invoked from a background thread with the path of the new
    file; callers must marshal to Fusion's main thread (a custom event).
    """

    def __init__(self, folder: str, callback: Callable[[str], None]) -> None:
        self.folder = folder
        self.callback = callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: List[str] = []

    def start(self) -> None:
        if not sys.platform.startswith('win') or not os.path.isdir(self.folder):
            return
        self._seen = [p for p in (os.path.join(self.folder, n) for n in os.listdir(self.folder))
                      if p.lower().endswith(STOCK_EXTENSIONS)]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info('watching %s for saved stock files' % self.folder)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        FILE_NOTIFY_CHANGE_FILE_NAME = 0x1
        FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
        WAIT_OBJECT_0 = 0
        k32 = ctypes.windll.kernel32
        k32.FindFirstChangeNotificationW.restype = ctypes.c_void_p
        k32.FindNextChangeNotification.argtypes = [ctypes.c_void_p]
        k32.FindCloseChangeNotification.argtypes = [ctypes.c_void_p]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        handle = k32.FindFirstChangeNotificationW(self.folder, False,
                                                  FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        try:
            while not self._stop.is_set():
                if k32.WaitForSingleObject(handle, 500) == WAIT_OBJECT_0:
                    time.sleep(1.0)  # let Simulation finish writing
                    self._check()
                    k32.FindNextChangeNotification(handle)
        finally:
            k32.FindCloseChangeNotification(handle)

    def _check(self) -> None:
        try:
            names = [os.path.join(self.folder, n) for n in os.listdir(self.folder)]
        except OSError:
            return
        for path in names:
            if path.lower().endswith(STOCK_EXTENSIONS) and path not in self._seen:
                self._seen.append(path)
                if _is_complete(path):
                    self.callback(path)


def _is_complete(path: str) -> bool:
    """A file whose size stopped changing and that can be opened for reading."""
    try:
        size = os.path.getsize(path)
        time.sleep(0.3)
        if os.path.getsize(path) != size:
            return False
        with open(path, 'rb'):
            return size > 84
    except OSError:
        return False
