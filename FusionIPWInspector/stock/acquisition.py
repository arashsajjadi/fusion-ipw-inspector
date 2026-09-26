"""Orchestration: which provider to use, what is currently loaded, cleanup rules.

One ``StockSession`` per open document keeps the last ``StockResult`` so the
inspector dialog can be closed and reopened without re-exporting, and so the
temporary mesh can be removed before the document is saved.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

import adsk.cam
import adsk.core
import adsk.fusion

from ..core.stock_file import Box
from ..core.transform import SetupFrame, SetupFrameError
from ..diagnostics import log
from ..utils.fusion_units import wcs_matrix_rows_mm
from .post_export import PostStockProvider, model_bounding_box_mm
from .provider import SOURCE_NONE, StockResult
from .saved_file import SavedStockProvider
from .temporary_mesh import TemporaryMesh


class StockSession:
    """State for one document: providers, the loaded stock and its setup."""

    def __init__(self, doc: adsk.core.Document) -> None:
        self.doc_name = doc.name
        self.result: Optional[StockResult] = None
        self._design_cache: Optional[adsk.fusion.Design] = None

    # ------------------------------------------------------------ products
    @staticmethod
    def products(doc: adsk.core.Document):
        cam = design = None
        try:
            cam = adsk.cam.CAM.cast(doc.products.itemByProductType('CAMProductType'))
        except Exception:
            cam = None
        try:
            design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
        except Exception:
            design = None
        return cam, design

    # ------------------------------------------------------------ frames
    @staticmethod
    def frame_of(setup: adsk.cam.Setup) -> Optional[SetupFrame]:
        try:
            return SetupFrame.from_matrix_rows(wcs_matrix_rows_mm(setup.workCoordinateSystem), setup.name)
        except SetupFrameError as exc:
            log.error('bad WCS for setup %s' % setup.name, exc)
            return None

    @staticmethod
    def all_frames(cam: adsk.cam.CAM) -> List[SetupFrame]:
        frames = []
        for s in cam.setups:
            f = StockSession.frame_of(s)
            if f is not None:
                frames.append(f)
        return frames

    # ------------------------------------------------------------ acquire
    def acquire_current(self, doc: adsk.core.Document, setup: adsk.cam.Setup) -> StockResult:
        """Primary path: ask Fusion for the in-process stock of ``setup``."""
        cam, design = self.products(doc)
        if cam is None or design is None:
            return StockResult(SOURCE_NONE, setup.name, setup.operationId, error='this document has no Manufacture data')
        provider = PostStockProvider(cam, design)
        model_box = model_bounding_box_mm(setup)
        result = provider.acquire(setup, model_box)
        self._remember(doc, result)
        return result

    def load_saved(self, doc: adsk.core.Document, setup: adsk.cam.Setup, path: str) -> StockResult:
        """Fallback path: a file saved from Simulation."""
        cam, design = self.products(doc)
        if cam is None or design is None:
            return StockResult(SOURCE_NONE, setup.name, setup.operationId, error='this document has no Manufacture data')
        provider = SavedStockProvider(design)
        model_box = model_bounding_box_mm(setup)
        result = provider.load(path, setup.name, setup.operationId, model_box, self.all_frames(cam))
        self._remember(doc, result)
        return result

    def _remember(self, doc: adsk.core.Document, result: StockResult) -> None:
        if result.ok:
            self.result = result
        elif self.result is not None and not self._mesh_alive(doc):
            self.result = None
        log.info('stock session: %s (%s)' % (result.label(), result.provider))

    # ------------------------------------------------------------ status
    def current_for(self, doc: adsk.core.Document, setup: adsk.cam.Setup) -> Optional[StockResult]:
        """The loaded stock if it belongs to ``setup`` and its mesh still exists."""
        r = self.result
        if r is None or r.setup_id != setup.operationId:
            return None
        if not self._mesh_alive(doc):
            self.result = None
            return None
        return r

    def _mesh_alive(self, doc: adsk.core.Document) -> bool:
        r = self.result
        if r is None:
            return False
        _, design = self.products(doc)
        if design is None:
            return False
        return TemporaryMesh(design).find_current(r.setup_id, r.fingerprint) is not None

    def remove_mesh(self, doc: adsk.core.Document) -> int:
        _, design = self.products(doc)
        removed = TemporaryMesh(design).remove_all() if design is not None else 0
        self.result = None
        return removed

    def is_temporary_entity(self, doc: adsk.core.Document, entity) -> bool:
        _, design = self.products(doc)
        return design is not None and TemporaryMesh(design).is_temporary_entity(entity)


class StockSessions:
    """All per-document sessions plus the document-save hook."""

    def __init__(self, app: adsk.core.Application) -> None:
        self.app = app
        self._sessions: Dict[str, StockSession] = {}
        self._handlers: List[adsk.core.EventHandler] = []

    def for_document(self, doc: adsk.core.Document) -> StockSession:
        key = doc.name
        session = self._sessions.get(key)
        if session is None:
            session = StockSession(doc)
            self._sessions[key] = session
        return session

    def hook_document_events(self) -> None:
        handler = _DocumentSavingHandler(self)
        self.app.documentSaving.add(handler)
        self._handlers.append(handler)

    def unhook(self) -> None:
        for h in self._handlers:
            try:
                self.app.documentSaving.remove(h)
            except Exception:
                pass
        self._handlers.clear()

    def remove_all_meshes(self) -> None:
        for i in range(self.app.documents.count):
            doc = self.app.documents.item(i)
            try:
                _, design = StockSession.products(doc)
                if design is not None:
                    TemporaryMesh(design).remove_all()
            except Exception as exc:
                log.error('cleanup in %s failed' % doc.name, exc)
        for s in self._sessions.values():
            s.result = None


class _DocumentSavingHandler(adsk.core.DocumentEventHandler):
    """Remove the temporary stock right before a save so it never lands in the file."""

    def __init__(self, owner: StockSessions) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.DocumentEventArgs) -> None:
        try:
            doc = args.document
            session = self.owner.for_document(doc)
            removed = session.remove_mesh(doc)
            if removed:
                log.info('temporary stock removed before saving %s' % doc.name)
        except Exception as exc:
            log.error('documentSaving cleanup failed', exc)
