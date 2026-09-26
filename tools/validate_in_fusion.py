"""Analytic validation of Fusion IPW Inspector, run inside Fusion as a script.

Builds a throw-away document (never saved), machines a pocket into a block in
Setup 1, lets Setup 2 use the preceding stock with a translated WCS, exports
Setup 2's in-process stock through the add-in's own provider and compares the
mesh against analytically known geometry.

Install: copy this file under
  %APPDATA%\\Autodesk\\Autodesk Fusion 360\\API\\Scripts\\IPWValidate\\IPWValidate.py
with a script manifest, run it from Utilities > Add-Ins, and read
  %LOCALAPPDATA%\\FusionIPWInspector\\validation_report.txt

Geometry (mm, world): block 40 x 30 x 20 with its corner at the origin and a
pocket x 5..25, y 5..15, z 12..20 (8 deep). Setup 1: relative box stock,
1 mm on the sides and top, 0 at the bottom, so the raw stock is
(-1..41, -1..31, 0..21); one 3D adaptive clearing with a small end mill and
zero stock to leave. Setup 2 (From preceding setup) with its WCS at the
stock's top corner must see a stock with the pocket cut into it: the pocket
floor at z = 12, the outer faces untouched.
"""
import importlib
import os
import struct
import sys
import time
import traceback

import adsk.cam
import adsk.core
import adsk.fusion

ADDIN_DIR = os.path.join(os.environ['APPDATA'], 'Autodesk', 'Autodesk Fusion 360', 'API', 'AddIns')
REPORT = os.path.join(os.environ['LOCALAPPDATA'], 'FusionIPWInspector', 'validation_report.txt')
TOL_PLANE_MM = 0.15
TOL_ANALYTIC_MM = 0.005
KEEP_DOCUMENT = os.path.exists(os.path.join(os.environ['LOCALAPPDATA'], 'FusionIPWInspector', 'keep_validation_doc'))


def _import_addin():
    if ADDIN_DIR not in sys.path:
        sys.path.insert(0, ADDIN_DIR)
    for name in list(sys.modules):
        if name.startswith('FusionIPWInspector'):
            del sys.modules[name]
    importlib.import_module('FusionIPWInspector')
    return (importlib.import_module('FusionIPWInspector.stock.post_export'),
            importlib.import_module('FusionIPWInspector.core.transform'),
            importlib.import_module('FusionIPWInspector.utils.fusion_units'))


def _vertices(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    count = struct.unpack('<I', data[80:84])[0]
    verts = []
    for tri in struct.iter_unpack('<12fH', data[84:84 + count * 50]):
        v = tri[3:12]
        verts.append(v[0:3]); verts.append(v[3:6]); verts.append(v[6:9])
    return verts


def _find_tool(cam_mgr, max_diameter_mm):
    """Largest flat end mill of a shipped metric sample library not larger than max_diameter_mm."""
    libs = cam_mgr.libraryManager.toolLibraries
    url = libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation)
    best, best_d = None, 0.0
    for lib_url in libs.childAssetURLs(url):
        if 'metric' not in lib_url.toString().lower():
            continue
        lib = libs.toolLibraryAtURL(lib_url)
        for i in range(lib.count):
            tool = lib.item(i)
            try:
                if tool.parameters.itemByName('tool_type').value.value != 'flat end mill':
                    continue
                d = tool.parameters.itemByName('tool_diameter').value.value * 10.0
                if d <= max_diameter_mm and d > best_d:
                    best, best_d = tool, d
            except Exception:
                continue
    return best, best_d


def _wait(future, seconds=180):
    t0 = time.time()
    while not future.isGenerationCompleted and time.time() - t0 < seconds:
        adsk.doEvents()
        time.sleep(0.2)
    return time.time() - t0


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    L = []
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, bool(ok), detail))
        L.append('%s %s%s' % ('PASS' if ok else 'FAIL', name, (' - ' + detail) if detail else ''))

    doc = None
    try:
        post_export, transform, fusion_units = _import_addin()
        doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        design = adsk.fusion.Design.cast(app.activeProduct)
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        design.unitsManager.distanceDisplayUnits = adsk.fusion.DistanceUnits.MillimeterDistanceUnits
        root = design.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(adsk.core.Point3D.create(0, 0, 0), adsk.core.Point3D.create(4.0, 3.0, 0))
        ext = root.features.extrudeFeatures.addSimple(sk.profiles.item(0), adsk.core.ValueInput.createByReal(2.0),
                                                      adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        body = ext.bodies.item(0)
        body.name = 'Block'
        top = None
        for f in body.faces:
            if f.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType and abs(f.centroid.z - 2.0) < 1e-6:
                top = f
        sk2 = root.sketches.add(top)
        sk2.sketchCurves.sketchLines.addTwoPointRectangle(adsk.core.Point3D.create(0.5, 0.5, 0), adsk.core.Point3D.create(2.5, 1.5, 0))
        prof = None
        for p in sk2.profiles:
            bb = p.boundingBox
            if abs((bb.maxPoint.x - bb.minPoint.x) - 2.0) < 1e-6:
                prof = p
        root.features.extrudeFeatures.addSimple(prof, adsk.core.ValueInput.createByReal(-0.8),
                                                adsk.fusion.FeatureOperations.CutFeatureOperation)
        ui.workspaces.itemById('CAMEnvironment').activate()
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType('CAMProductType'))
        cam_mgr = adsk.cam.CAMManager.get()

        # ---- Setup 1: relative box stock (1 mm sides/top), 3D adaptive clearing
        inp = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
        inp.models = [body]
        inp.name = 'Setup1_pocket'
        inp.stockMode = adsk.cam.SetupStockModes.RelativeBoxStock
        s1 = cam.setups.add(inp)
        s1.parameters.itemByName('job_stockOffsetSides').expression = '1 mm'
        s1.parameters.itemByName('job_stockOffsetTop').expression = '1 mm'
        s1.parameters.itemByName('job_stockOffsetBottom').expression = '0 mm'
        tool, tool_d = _find_tool(cam_mgr, 4.0)
        check('sample tool found (<= 4 mm flat end mill)', tool is not None, '%.1f mm' % tool_d)
        op_in = s1.operations.createInput('adaptive')
        op_in.tool = tool
        op_in.displayName = 'Adaptive pocket'
        op1 = s1.operations.add(op_in)
        for pname, expr in (('useStockToLeave', 'false'), ('optimalLoad', '1 mm'), ('maximumStepdown', '2 mm'),
                            ('tolerance', '0.01 mm')):
            try:
                op1.parameters.itemByName(pname).expression = expr
            except Exception as e:
                L.append('  param %s: %s' % (pname, str(e).splitlines()[0][:80]))
        secs = _wait(cam.generateToolpath(op1))
        check('Setup1 adaptive toolpath generated', op1.hasToolpath and op1.isToolpathValid, '%.1f s' % secs)

        # ---- Setup 2: from preceding setup, WCS at the stock top corner (translated frame)
        inp2 = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
        inp2.models = [body]
        inp2.name = 'Setup2_ipw'
        inp2.stockMode = adsk.cam.SetupStockModes.PreviousSetupStock
        s2 = cam.setups.add(inp2)
        for pname, val in (('wcs_orientation_mode', 'modelOrientation'), ('wcs_origin_mode', 'stockPoint'),
                           ('wcs_origin_boxPoint', 'top 1')):
            adsk.cam.ChoiceParameterValue.cast(s2.parameters.itemByName(pname).value).value = val
        # The Setup dialog sets this together with "From preceding setup"; the API's stockMode does not.
        # Without it Fusion never computes the incoming stock for the setup.
        s2.parameters.itemByName('job_continueMachining').expression = 'true'
        op2_in = s2.operations.createInput('face')
        op2_in.tool = tool
        op2_in.displayName = 'Face'
        op2 = s2.operations.add(op2_in)
        secs = _wait(cam.generateToolpath(op2))
        check('Setup2 operation generated', op2.hasToolpath and op2.isToolpathValid, '%.1f s' % secs)
        frame2 = transform.SetupFrame.from_matrix_rows(fusion_units.wcs_matrix_rows_mm(s2.workCoordinateSystem), s2.name)
        L.append('Setup2 WCS: ' + frame2.describe())
        check('Setup2 WCS is translated (not at world origin)', not frame2.is_identity(), frame2.describe())

        # ---- export Setup2's IPW through the add-in provider (retry while Fusion generates IPS in the background)
        provider = post_export.PostStockProvider(cam, design)
        t0 = time.time()
        result = None
        for attempt in range(5):
            result = provider.acquire(s2, post_export.model_bounding_box_mm(s2))
            if result.ok and not result.is_plain_box:
                break
            adsk.doEvents()
            time.sleep(2.0)
        L.append('export attempts: %d, elapsed %.1f s' % (attempt + 1, time.time() - t0))
        check('provider acquired current IPW', result.ok, result.error or result.label())
        if result.ok:
            L.append('stock file: %s (%d triangles, mean edge %.3f mm, unit scale %s)' % (
                result.stock_path, result.triangle_count, result.mean_edge_mm, result.unit_scale_mm))
            world = [frame2.setup_to_world(tuple(c * result.unit_scale_mm for c in v)) for v in _vertices(result.stock_path)]
            xs = [p[0] for p in world]; ys = [p[1] for p in world]; zs = [p[2] for p in world]
            check('IPW is machined (more than a 12-triangle box)', result.triangle_count > 12, '%d triangles' % result.triangle_count)
            # 3D adaptive clears the whole outside of the block too, so the IPW outline is the model box.
            check('IPW outline x 0..40 (model, sides cleared)', abs(min(xs)) <= TOL_PLANE_MM and abs(max(xs) - 40.0) <= TOL_PLANE_MM, 'x %.3f..%.3f' % (min(xs), max(xs)))
            check('IPW outline y 0..30', abs(min(ys)) <= TOL_PLANE_MM and abs(max(ys) - 30.0) <= TOL_PLANE_MM, 'y %.3f..%.3f' % (min(ys), max(ys)))
            check('IPW outline z 0..20', abs(min(zs)) <= TOL_PLANE_MM and abs(max(zs) - 20.0) <= TOL_PLANE_MM, 'z %.3f..%.3f' % (min(zs), max(zs)))
            cavity = [p for p in world if 7.0 < p[0] < 23.0 and 7.0 < p[1] < 13.0 and p[2] < 20.5]
            floor = [p for p in cavity if p[2] < 12.5]
            check('pocket cavity present in the IPW', len(cavity) > 50, '%d vertices inside the pocket footprint' % len(cavity))
            if floor:
                zf = sorted(p[2] for p in floor)
                zmed = zf[len(zf) // 2]
                check('pocket floor at z = 12 (analytic: 20 - 8)', abs(zmed - 12.0) <= TOL_PLANE_MM, 'median floor z = %.3f, min %.3f' % (zmed, zf[0]))
            else:
                check('pocket floor at z = 12 (analytic: 20 - 8)', False, 'no floor vertices found')
            if result.part_path and os.path.isfile(result.part_path):
                pv = [frame2.setup_to_world(tuple(c * result.unit_scale_mm for c in v)) for v in _vertices(result.part_path)]
                pmin = tuple(min(p[i] for p in pv) for i in range(3)); pmax = tuple(max(p[i] for p in pv) for i in range(3))
                dev = max(max(abs(pmin[i] - (0, 0, 0)[i]), abs(pmax[i] - (40, 30, 20)[i])) for i in range(3))
                check('exported part matches the model box within 0.005 mm', dev <= TOL_ANALYTIC_MM, 'max deviation %.4f mm' % dev)
            occs = provider.temporary.find_all()
            check('temporary mesh component created', len(occs) == 1)
            if occs:
                mb = occs[0].component.meshBodies.item(0)
                check('temporary mesh triangle count matches file', mb.mesh.triangleCount == result.triangle_count,
                      '%d vs %d' % (mb.mesh.triangleCount, result.triangle_count))
                # MeshBody.boundingBox is in the component's own frame; the occurrence transform places it.
                fv = _vertices(result.stock_path)
                fmin = tuple(min(v[i] for v in fv) * result.unit_scale_mm for i in range(3))
                fmax = tuple(max(v[i] for v in fv) * result.unit_scale_mm for i in range(3))
                bb = mb.boundingBox
                local = ((bb.minPoint.x * 10, bb.minPoint.y * 10, bb.minPoint.z * 10), (bb.maxPoint.x * 10, bb.maxPoint.y * 10, bb.maxPoint.z * 10))
                dev = max(max(abs(local[0][i] - fmin[i]), abs(local[1][i] - fmax[i])) for i in range(3))
                check('mesh body bounding box equals the file bounding box (setup frame)', dev <= 0.01, 'largest deviation %.4f mm' % dev)
                tr = occs[0].transform.asArray()
                origin_dev = max(abs(tr[3] * 10 - frame2.origin[0]), abs(tr[7] * 10 - frame2.origin[1]), abs(tr[11] * 10 - frame2.origin[2]))
                axes_dev = max(abs(tr[i] - v) for i, v in zip((0, 4, 8, 1, 5, 9, 2, 6, 10), frame2.axis_x + frame2.axis_y + frame2.axis_z))
                check('occurrence transform equals the setup WCS (origin and axes)', origin_dev <= 1e-6 and axes_dev <= 1e-9,
                      'origin deviation %.6f mm, axes deviation %.2e' % (origin_dev, axes_dev))
            if not KEEP_DOCUMENT:
                provider.temporary.remove_all()
        p = (40.0, 30.0, 20.0)
        s = frame2.world_to_setup(p)
        check('world->setup->world round trip', max(abs(a - b) for a, b in zip(frame2.setup_to_world(s), p)) <= 1e-9,
              'setup coords %s' % (tuple(round(c, 3) for c in s),))
    except Exception:
        L.append('EXC: ' + traceback.format_exc())
        checks.append(('script ran to completion', False, 'exception'))
    finally:
        try:
            if doc is not None and not KEEP_DOCUMENT:
                doc.close(False)
        except Exception:
            pass
    failed = [c for c in checks if not c[1]]
    head = 'Fusion IPW Inspector validation: %d checks, %d failed' % (len(checks), len(failed))
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, 'w', encoding='utf-8') as fh:
        fh.write(head + '\n' + '\n'.join(L) + '\n')
    ui.messageBox(head + '\nReport: ' + REPORT, 'IPW Inspector validation')
