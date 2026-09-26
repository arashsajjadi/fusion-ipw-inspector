"""The pickable temporary mesh component.

Whatever provider produced the stock, it ends up here: one component in the
root of the design, clearly named, tagged with attributes so that only
components created by this add-in are ever removed, and placed with the
setup->world transform so picked points come back in world space.

The component is never meant to be saved: it is removed before the document
is saved (the add-in hooks ``documentSaving``) and when the add-in stops.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import adsk.core
import adsk.fusion

from ..core.transform import SetupFrame
from ..diagnostics import log
from ..utils.fusion_units import API_CM_TO_MM

ATTRIBUTE_GROUP = 'FusionIPWInspector'
ATTRIBUTE_TEMP = 'temporaryStock'
ATTRIBUTE_FINGERPRINT = 'fingerprint'
ATTRIBUTE_SETUP = 'setupId'
COMPONENT_NAME = 'IPW Inspector temporary stock (not saved, safe to delete)'
BODY_NAME = 'In-process stock'

MESH_UNITS = {1.0: adsk.fusion.MeshUnits.MillimeterMeshUnit,
              10.0: adsk.fusion.MeshUnits.CentimeterMeshUnit,
              25.4: adsk.fusion.MeshUnits.InchMeshUnit}


class TemporaryMesh:
    """Creates, finds and removes the add-in's temporary stock component."""

    def __init__(self, design: adsk.fusion.Design) -> None:
        self.design = design

    # ------------------------------------------------------------- lookup
    def find_all(self) -> List[adsk.fusion.Occurrence]:
        found = []
        try:
            for occ in self.design.rootComponent.allOccurrences:
                if occ.component.attributes.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_TEMP):
                    found.append(occ)
        except Exception as exc:
            log.error('scanning for temporary stock failed', exc)
        return found

    def find_current(self, setup_id: int, fingerprint: str) -> Optional[adsk.fusion.Occurrence]:
        """The temporary component holding exactly this stock, if it still exists."""
        for occ in self.find_all():
            try:
                attrs = occ.component.attributes
                fp = attrs.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_FINGERPRINT)
                sid = attrs.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_SETUP)
                if fp and sid and fp.value == fingerprint and sid.value == str(setup_id) \
                        and occ.component.meshBodies.count > 0:
                    return occ
            except Exception:
                continue
        return None

    def mesh_body(self, occ: adsk.fusion.Occurrence) -> Optional[adsk.fusion.MeshBody]:
        try:
            if occ.component.meshBodies.count:
                return occ.component.meshBodies.item(0)
        except Exception:
            pass
        return None

    def is_temporary_entity(self, entity) -> bool:
        """True when a picked entity belongs to the temporary stock component."""
        try:
            mesh = adsk.fusion.MeshBody.cast(entity)
            if mesh is None:
                return False
            comp = mesh.parentComponent
            return bool(comp and comp.attributes.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_TEMP))
        except Exception:
            return False

    # ------------------------------------------------------------- change
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

    def import_mesh(self, path: str, unit_scale_mm: float, frame: SetupFrame, setup_id: int,
                    fingerprint: str, body_name: str = BODY_NAME) -> Tuple[adsk.fusion.Occurrence, adsk.fusion.MeshBody]:
        """Import an STL expressed in ``frame`` coordinates as the temporary component."""
        root = self.design.rootComponent
        transform = adsk.core.Matrix3D.create()
        if not frame.is_identity():
            rows = frame.matrix_rows()
            for i in (3, 7, 11):
                rows[i] /= API_CM_TO_MM
            transform.setWithArray(rows)
        occ = root.occurrences.addNewComponent(transform)
        comp = occ.component
        comp.name = COMPONENT_NAME
        attrs = comp.attributes
        attrs.add(ATTRIBUTE_GROUP, ATTRIBUTE_TEMP, '1')
        attrs.add(ATTRIBUTE_GROUP, ATTRIBUTE_SETUP, str(setup_id))
        attrs.add(ATTRIBUTE_GROUP, ATTRIBUTE_FINGERPRINT, fingerprint)
        base_feature = None
        if self.design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            base_feature = comp.features.baseFeatures.add()
            base_feature.startEdit()
        try:
            meshes = comp.meshBodies.add(path, MESH_UNITS.get(unit_scale_mm, MESH_UNITS[1.0]), base_feature)
        finally:
            if base_feature is not None:
                base_feature.finishEdit()
        if not meshes or meshes.count == 0:
            occ.deleteMe()
            raise RuntimeError('Fusion could not import the stock mesh file.')
        mesh = meshes.item(0)
        try:
            mesh.name = body_name
        except Exception:
            pass
        _style(mesh)
        return occ, mesh


def _style(mesh: adsk.fusion.MeshBody) -> None:
    """Calm display: no triangle edges, slightly translucent so the model shows through.

    Both settings are display-only properties of the temporary body and are
    removed with it. Failures are ignored (older builds may lack the overrides).
    """
    try:
        mesh.displayOverrides.isSuppressTriangleEdges = True
    except Exception as exc:
        log.info('mesh display override not available: %s' % exc)
    try:
        mesh.opacity = 0.85
    except Exception as exc:
        log.info('mesh opacity not available: %s' % exc)
