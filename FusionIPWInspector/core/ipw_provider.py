"""In-process stock (IPW) acquisition.

Fusion (build 2.0.2705x, September 2026) still has no public API that returns
the simulated / in-process stock. The only supported way to get it out is the
Simulation context menu "Stock > Save Stock..." which writes an STL file. This
module therefore provides:

* ``StockFileProvider`` - imports such a file into a clearly named temporary
  component so it can be picked in the viewport, using ``stock_file`` to work
  out the file's unit and coordinate frame from the setup's model geometry.
* ``TemporaryStock`` bookkeeping - everything the add-in creates is tagged with
  an attribute so it can be found and removed later, even after a crash.

The transform layer (``setup_transform``) does not depend on anything here; if
Autodesk exposes the IPW directly one day only this module needs replacing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import adsk.core
import adsk.fusion

from ..utils import log
from ..utils.fusion_units import API_CM_TO_MM
from .setup_transform import SetupFrame
from .stock_file import Box, StockFileError, StockInterpretation, infer_interpretation, read_stock_file

ATTRIBUTE_GROUP = 'FusionIPWInspector'
ATTRIBUTE_TEMP = 'temporaryStock'
TEMP_COMPONENT_NAME = 'IPW Inspector temporary stock (safe to delete)'
TEMP_BODY_NAME = 'IPW stock'

StockError = StockFileError


@dataclass
class LoadedStock:
    occurrence: adsk.fusion.Occurrence
    mesh_body: adsk.fusion.MeshBody
    file_path: str
    file_mtime: float
    interpretation: StockInterpretation
    notes: List[str] = field(default_factory=list)


def model_bounding_box_mm(setup) -> Optional[Box]:
    """World-space bounding box (mm) of the bodies/occurrences a setup machines."""
    mins = [float('inf')] * 3
    maxs = [float('-inf')] * 3
    found = False
    try:
        models = setup.models
    except Exception:
        return None
    for m in models:
        for bb in _bounding_boxes(m):
            found = True
            for i, (lo, hi) in enumerate(((bb.minPoint.x, bb.maxPoint.x), (bb.minPoint.y, bb.maxPoint.y),
                                         (bb.minPoint.z, bb.maxPoint.z))):
                mins[i] = min(mins[i], lo * API_CM_TO_MM)
                maxs[i] = max(maxs[i], hi * API_CM_TO_MM)
    return (tuple(mins), tuple(maxs)) if found else None


def _bounding_boxes(entity):
    occ = adsk.fusion.Occurrence.cast(entity)
    if occ:
        for b in occ.bRepBodies:
            yield b.boundingBox
        for mb in occ.component.meshBodies:
            yield mb.boundingBox
        return
    body = adsk.fusion.BRepBody.cast(entity)
    if body:
        yield body.boundingBox
        return
    mesh = adsk.fusion.MeshBody.cast(entity)
    if mesh:
        yield mesh.boundingBox


# ---------------------------------------------------------- temporary stock
class TemporaryStock:
    """Creates, finds and removes the add-in's temporary stock component."""

    def __init__(self, design: adsk.fusion.Design) -> None:
        self.design = design

    def find_all(self) -> List[adsk.fusion.Occurrence]:
        found = []
        try:
            for occ in self.design.rootComponent.allOccurrences:
                if occ.component.attributes.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_TEMP):
                    found.append(occ)
        except Exception as exc:
            log.error('scanning for temporary stock failed', exc)
        return found

    def remove_all(self) -> int:
        removed = 0
        for occ in self.find_all():
            try:
                occ.deleteMe()
                removed += 1
            except Exception as exc:
                log.error('could not delete temporary stock component', exc)
        if removed:
            log.info('removed %d temporary stock component(s)' % removed)
        return removed

    def import_mesh(self, path: str, units: int, frame: SetupFrame) -> Tuple[adsk.fusion.Occurrence, adsk.fusion.MeshBody]:
        root = self.design.rootComponent
        transform = adsk.core.Matrix3D.create()
        if not frame.is_identity():
            rows = frame.matrix_rows()
            for i in (3, 7, 11):
                rows[i] /= API_CM_TO_MM
            transform.setWithArray(rows)
        occ = root.occurrences.addNewComponent(transform)
        comp = occ.component
        comp.name = TEMP_COMPONENT_NAME
        comp.attributes.add(ATTRIBUTE_GROUP, ATTRIBUTE_TEMP, '1')
        base_feature = None
        if self.design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            base_feature = comp.features.baseFeatures.add()
            base_feature.startEdit()
        try:
            meshes = comp.meshBodies.add(path, units, base_feature)
        finally:
            if base_feature is not None:
                base_feature.finishEdit()
        if not meshes or meshes.count == 0:
            occ.deleteMe()
            raise StockError('Fusion could not import the stock mesh. Save the stock again from Simulation as an STL file.')
        mesh = meshes.item(0)
        try:
            mesh.name = TEMP_BODY_NAME
        except Exception:
            pass
        return occ, mesh


# ------------------------------------------------------------------ provider
class StockFileProvider:
    """Loads a Save Stock file (STL) as pickable temporary geometry."""

    MESH_UNITS = {1.0: adsk.fusion.MeshUnits.MillimeterMeshUnit,
                  10.0: adsk.fusion.MeshUnits.CentimeterMeshUnit,
                  25.4: adsk.fusion.MeshUnits.InchMeshUnit}

    def __init__(self, design: adsk.fusion.Design) -> None:
        self.design = design
        self.temporary = TemporaryStock(design)
        self.current: Optional[LoadedStock] = None

    def load(self, path: str, setup, all_setups_frames: Sequence[SetupFrame]) -> LoadedStock:
        if not os.path.isfile(path):
            raise StockError('The stock file no longer exists:\n%s' % path)
        mtime = os.path.getmtime(path)
        if self.current and self.current.file_path == path and self.current.file_mtime == mtime \
                and self._is_alive(self.current):
            log.info('stock already loaded and unchanged; reusing it')
            return self.current
        info = read_stock_file(path)
        model_box = model_bounding_box_mm(setup)
        interp = infer_interpretation(info.bbox, model_box, all_setups_frames)
        log.info('stock %s: %d triangles, bbox %s, interpreted as %s (score %.2f)' % (
            os.path.basename(path), info.triangle_count, info.bbox, interp.label, interp.score))
        self.temporary.remove_all()
        occ, mesh = self.temporary.import_mesh(path, self.MESH_UNITS[interp.scale_to_mm], interp.frame)
        notes = []
        if model_box is not None and interp.score < 0.5:
            notes.append('The stock does not enclose the setup model, so it may be the wrong file or '
                         'from another setup. Coordinates are still reported in the selected setup WCS.')
        self.current = LoadedStock(occ, mesh, path, mtime, interp, notes)
        log.info('mesh detail: average triangle edge %.3f mm' % (info.mean_edge * interp.scale_to_mm))
        return self.current

    def unload(self) -> None:
        self.temporary.remove_all()
        self.current = None

    def _is_alive(self, loaded: LoadedStock) -> bool:
        try:
            return loaded.occurrence.isValid and loaded.mesh_body.isValid
        except Exception:
            return False
