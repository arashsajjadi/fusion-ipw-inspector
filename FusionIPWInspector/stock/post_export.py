"""Primary provider: let Fusion's post engine export the in-process stock.

How it works (documented post-processor behaviour, no UI involved):

1. A tiny helper post (``resources/post/ipw_inspector_stock.cps``) declares
   ``this.exportStock = true`` / ``this.exportPart = true``. For such posts
   Fusion exports the setup's stock and part as STL before running the post
   and passes the paths as the global parameters ``autodeskcam:stock-path``
   and ``autodeskcam:part-path`` (the same mechanism the shipped CAMplete post
   uses for machine simulation). For a "From preceding setup" setup the stock
   is the in-process stock left by the preceding setups.
2. The helper post copies both files next to its output and writes a
   key=value sidecar; it emits no NC code.
3. This provider installs the helper post into the personal post folder when
   needed, creates a temporary NC program for the setup, posts it into the
   add-in's cache folder, reads the sidecar and deletes the NC program again.
   All of that happens inside one Fusion command so it is a single undo step.

The exported mesh is expressed in the setup's WCS (millimetres or inches,
following the document unit), which the sidecar reports.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from typing import Dict, Optional, Tuple

import adsk.cam
import adsk.core
import adsk.fusion

from ..core.stock_file import Box, read_stock_file
from ..core.transform import SetupFrame, SetupFrameError
from ..diagnostics import log
from ..utils.fusion_units import API_CM_TO_MM, wcs_matrix_rows_mm
from . import mesh_validation
from .provider import SOURCE_CURRENT, SOURCE_NONE, StockError, StockResult
from .temporary_mesh import TemporaryMesh

POST_FILE_NAME = 'ipw_inspector_stock.cps'
POST_URL = 'user://' + POST_FILE_NAME
POST_DESCRIPTION = 'IPW Inspector stock export'
POST_RESOURCE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources', 'post', POST_FILE_NAME)
CACHE_ROOT = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'FusionIPWInspector', 'cache')
NC_PROGRAM_NAME = 'IPW Inspector (temporary, removed automatically)'
UNIT_SCALE = {'mm': 1.0, 'in': 25.4}


def _safe_name(text: str) -> str:
    return ''.join(ch if ch.isalnum() else '_' for ch in text)[:60] or 'setup'


def _file_fingerprint(path: str) -> str:
    """Content fingerprint of a stock file, insensitive to what Fusion leaves undefined.

    Fusion's binary STL export writes identical normals and vertices for an
    unchanged stock but uninitialised 2-byte attribute fields, so those bytes
    are zeroed before hashing (an extended slice assignment on a bytearray, so
    the whole 40 MB file hashes in well under a second). The same stock then
    keeps its fingerprint across exports, which is what lets the snap cache be
    reused across dialog sessions and Fusion restarts.
    """
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        data = bytearray(fh.read())
    h.update(str(len(data)).encode())
    if len(data) >= 84 and not (data[:5].lower() == b'solid' and b'facet' in data[:4096]):
        count = int.from_bytes(data[80:84], 'little')
        end = 84 + count * 50
        if end <= len(data):
            zeros = bytes(count)
            data[132:end:50] = zeros      # first attribute byte of every record (offset 48 within the record)
            data[133:end:50] = zeros      # second attribute byte
            h.update(data[:80] + data[84:end])
            h.update(data[end:])
            return h.hexdigest()[:16]
    h.update(data)
    return h.hexdigest()[:16]


class PostStockProvider:
    """Exports the current in-process stock of a setup through the post engine."""

    name = 'post-export'

    def __init__(self, cam: adsk.cam.CAM, design: adsk.fusion.Design) -> None:
        self.cam = cam
        self.design = design
        self.temporary = TemporaryMesh(design)

    # ------------------------------------------------------------ helper post
    def ensure_post_installed(self) -> str:
        """Copy the helper post into the personal post folder if it is missing or outdated."""
        folder = self.cam.personalPostFolder
        if not folder:
            raise StockError('Fusion did not report a personal post folder, so the stock export post cannot be installed.')
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, POST_FILE_NAME)
        with open(POST_RESOURCE, 'rb') as fh:
            wanted = fh.read()
        current = b''
        if os.path.isfile(target):
            with open(target, 'rb') as fh:
                current = fh.read()
        if current != wanted:
            shutil.copyfile(POST_RESOURCE, target)
            log.info('helper post installed to %s' % target)
        return target

    def post_configuration(self) -> adsk.cam.PostConfiguration:
        lib = adsk.cam.CAMManager.get().libraryManager.postLibrary
        try:
            cfg = lib.postConfigurationAtURL(adsk.core.URL.create(POST_URL))
            if cfg:
                return cfg
        except Exception as exc:
            log.info('post lookup by URL failed (%s); querying the local library' % exc)
        for cfg in lib.createQuery(adsk.cam.LibraryLocations.LocalLibraryLocation).execute():
            try:
                if cfg.description.startswith(POST_DESCRIPTION):
                    return cfg
            except Exception:
                continue
        raise StockError('The stock export post was installed but Fusion\'s post library does not list it yet. '
                         'Open Manage > Post Library once, then try again.')

    # ------------------------------------------------------------- export
    def export(self, setup: adsk.cam.Setup) -> Tuple[str, Dict[str, str]]:
        """Run the helper post for ``setup``. Returns (sidecar path, parsed sidecar)."""
        generated = 0
        for op in setup.allOperations:
            try:
                if op.hasToolpath and not op.isSuppressed:
                    generated += 1
            except Exception:
                continue
        if generated == 0:
            raise StockError('%s has no generated toolpath yet. Fusion exports the in-process stock only while '
                             'posting a setup with at least one generated operation: add or generate one '
                             '(Actions > Generate), then use Advanced > Refresh IPW.' % setup.name)
        self.ensure_post_installed()
        cfg = self.post_configuration()
        doc_key = _safe_name(self.design.parentDocument.name if self.design.parentDocument else 'document')
        out_dir = os.path.join(CACHE_ROOT, doc_key)
        os.makedirs(out_dir, exist_ok=True)
        base = 'ipw_%s_%d' % (_safe_name(setup.name), setup.operationId)
        for old in os.listdir(out_dir):
            # Previous exports of this setup are replaced; the snap cache next to them stays
            # (it carries the stock fingerprint and is reused when the re-export is identical).
            if old.startswith(base) and not old.endswith('.snapcache'):
                try:
                    os.remove(os.path.join(out_dir, old))
                except OSError:
                    pass
        programs = self.cam.ncPrograms
        inp = programs.createInput()
        inp.displayName = NC_PROGRAM_NAME
        inp.operations = [setup]
        prm = inp.parameters
        prm.itemByName('nc_program_output_folder').expression = "'%s'" % out_dir.replace('\\', '/')
        prm.itemByName('nc_program_filename').value.value = base
        for name, value in (('nc_program_openInEditor', False), ('nc_program_postToFusionTeam', False)):
            try:
                prm.itemByName(name).value.value = value
            except Exception:
                pass
        program = programs.add(inp)
        started = time.time()
        try:
            program.postConfiguration = cfg
            options = adsk.cam.NCProgramPostProcessOptions.create()
            options.postProcessExecutionBehavior = adsk.cam.PostProcessExecutionBehaviors.PostProcessExecutionBehavior_PostAll
            ok = program.postProcess(options)
        finally:
            try:
                program.deleteMe()
            except Exception as exc:
                log.error('temporary NC program could not be deleted', exc)
        sidecar = os.path.join(out_dir, base + '.ipwinfo')
        if not ok or not os.path.isfile(sidecar):
            raise StockError('Fusion could not post-process %s to export its stock. Make sure the setup\'s '
                             'operations are generated (no red warnings) and try again.' % setup.name)
        info = self._read_sidecar(sidecar)
        log.info('post export for %s took %.2f s: %s' % (setup.name, time.time() - started,
                                                       {k: v for k, v in info.items() if 'file' in k or k in ('unit', 'status')}))
        return sidecar, info

    @staticmethod
    def _read_sidecar(path: str) -> Dict[str, str]:
        info: Dict[str, str] = {}
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if '=' in line:
                    key, value = line.rstrip('\r\n').split('=', 1)
                    info[key.strip()] = value.strip()
        return info

    # ------------------------------------------------------------ acquire
    def acquire(self, setup: adsk.cam.Setup, model_box_world_mm: Optional[Box]) -> StockResult:
        """Export, validate and import the in-process stock of ``setup``."""
        try:
            frame = SetupFrame.from_matrix_rows(wcs_matrix_rows_mm(setup.workCoordinateSystem), setup.name)
        except SetupFrameError as exc:
            return StockResult(SOURCE_NONE, setup.name, setup.operationId, error='the WCS of %s is not usable (%s)' % (setup.name, exc))
        try:
            _, info = self.export(setup)
        except StockError as exc:
            return StockResult(SOURCE_NONE, setup.name, setup.operationId, frame=frame, error=str(exc), provider=self.name)
        except Exception as exc:
            log.error('post export failed', exc)
            return StockResult(SOURCE_NONE, setup.name, setup.operationId, frame=frame, provider=self.name,
                               error='Fusion could not export the stock (%s).' % str(exc).splitlines()[0][:120])
        stock_path = info.get('stock-file', '')
        if info.get('status') != 'ok' or not os.path.isfile(stock_path):
            return StockResult(SOURCE_NONE, setup.name, setup.operationId, frame=frame, provider=self.name,
                               error='Fusion did not export a stock for %s. Check the setup\'s Stock tab.' % setup.name)
        unit_scale = UNIT_SCALE.get(info.get('unit', 'mm'), 1.0)
        stock_info = read_stock_file(stock_path)
        result = StockResult(SOURCE_CURRENT, setup.name, setup.operationId, frame=frame, stock_path=stock_path,
                             part_path=info.get('part-file', ''), unit_scale_mm=unit_scale,
                             triangle_count=stock_info.triangle_count,
                             mean_edge_mm=stock_info.mean_edge * unit_scale, provider=self.name,
                             is_plain_box=stock_info.triangle_count <= mesh_validation.PLAIN_BOX_TRIANGLES,
                             fingerprint=_file_fingerprint(stock_path))
        # Validation happens in the setup frame, where the file lives.
        model_box_setup = _box_to_frame(model_box_world_mm, frame) if model_box_world_mm else None
        part_box = None
        if result.part_path and os.path.isfile(result.part_path):
            try:
                part_info = read_stock_file(result.part_path)
                part_box = tuple(tuple(c * unit_scale for c in corner) for corner in part_info.bbox)
            except Exception:
                part_box = None
        expected = _expected_stock_box(setup)
        report = mesh_validation.validate_stock(stock_info, unit_scale, model_box_setup, expected, part_box,
                                               previous_setup_stock=setup.stockMode == adsk.cam.SetupStockModes.PreviousSetupStock)
        result.notes.extend(report.notes)
        if result.is_plain_box and setup.stockMode == adsk.cam.SetupStockModes.PreviousSetupStock:
            if not _continue_machining(setup):
                result.notes.append('This setup was created without "continue machining" (typical for API-created '
                                    'setups). Open the setup, reselect Stock > From preceding setup, generate, then refresh.')
            else:
                result.notes.append("Fusion computes the incoming stock when this setup's operations are generated; "
                                    'if they are out of date, generate them and use Refresh IPW.')
        if not report.ok:
            result.error = ' '.join(report.problems)
            result.source = SOURCE_NONE
            return result
        # Import (or reuse) the pickable mesh.
        existing = self.temporary.find_current(setup.operationId, result.fingerprint)
        if existing is not None:
            result.mesh_body_valid = True
            log.info('temporary stock for %s is unchanged; reusing it' % setup.name)
            return result
        self.temporary.remove_all()
        started = time.time()
        self.temporary.import_mesh(stock_path, unit_scale, frame, setup.operationId, result.fingerprint)
        result.mesh_body_valid = True
        log.info('imported %d triangles for %s in %.2f s' % (result.triangle_count, setup.name, time.time() - started))
        return result


# ------------------------------------------------------------------ helpers
def _box_to_frame(box_world: Box, frame: SetupFrame) -> Box:
    corners = [(x, y, z) for x in (box_world[0][0], box_world[1][0]) for y in (box_world[0][1], box_world[1][1])
               for z in (box_world[0][2], box_world[1][2])]
    local = [frame.world_to_setup(c) for c in corners]
    return (tuple(min(c[i] for c in local) for i in range(3)), tuple(max(c[i] for c in local) for i in range(3)))


def _continue_machining(setup: adsk.cam.Setup) -> bool:
    """The Setup dialog sets job_continueMachining together with "From preceding setup"."""
    try:
        return bool(setup.parameters.itemByName('job_continueMachining').value.value)
    except Exception:
        return True


def _expected_stock_box(setup: adsk.cam.Setup) -> Optional[Box]:
    """Fusion's stock box of the setup (setup frame, mm) from its parameters.

    Parameter values come back in the API's internal unit (cm), whatever the
    document unit is; the expressions are what the user sees.
    """
    try:
        prm = setup.parameters
        vals = []
        for name in ('stockXLow', 'stockYLow', 'stockZLow', 'stockXHigh', 'stockYHigh', 'stockZHigh'):
            vals.append(float(prm.itemByName(name).value.value) * API_CM_TO_MM)
        lo, hi = tuple(vals[:3]), tuple(vals[3:])
        if any(h <= l for l, h in zip(lo, hi)):
            return None
        return (lo, hi)
    except Exception:
        return None


def model_bounding_box_mm(setup: adsk.cam.Setup) -> Optional[Box]:
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
