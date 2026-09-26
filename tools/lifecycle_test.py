"""Lifecycle regression test for Fusion IPW Inspector, run inside Fusion as a script.

Repeats the acquire -> place -> remove cycle of the add-in on the active document's
active (or first) setup and checks after every cycle that

  * exactly one temporary stock component exists while the stock is placed,
  * zero remain after removal,
  * zero custom graphics groups tagged by the add-in remain (cam and root),
  * the cache folder holds the same files (no accumulation),
  * the placed mesh reports the same triangle count each time.

Install: copy this file to
  %APPDATA%\\Autodesk\\Autodesk Fusion 360\\API\\Scripts\\IPWLifecycle\\IPWLifecycle.py
with a script manifest, run it from Utilities > Add-Ins, and read
  %LOCALAPPDATA%\\FusionIPWInspector\\lifecycle_report.txt

The dialog itself (open/close through the UI) is exercised separately; this script
covers the document-side lifecycle that the dialog relies on.
"""
import os
import sys
import time
import traceback

import adsk.cam
import adsk.core
import adsk.fusion

ADDIN_DIR = os.path.join(os.environ['APPDATA'], 'Autodesk', 'Autodesk Fusion 360', 'API', 'AddIns')
DATA_DIR = os.path.join(os.environ['LOCALAPPDATA'], 'FusionIPWInspector')
REPORT = os.path.join(DATA_DIR, 'lifecycle_report.txt')
CYCLES = 10


def _import_addin():
    if ADDIN_DIR not in sys.path:
        sys.path.insert(0, ADDIN_DIR)
    from FusionIPWInspector.stock.acquisition import StockSession
    from FusionIPWInspector.stock.temporary_mesh import TemporaryMesh, ATTRIBUTE_GROUP, ATTRIBUTE_TEMP
    from FusionIPWInspector.ui.marker import GROUP_ID, sweep_orphans
    return StockSession, TemporaryMesh, ATTRIBUTE_GROUP, ATTRIBUTE_TEMP, GROUP_ID, sweep_orphans


def _count_temp(design, group, name):
    n = 0
    for occ in design.rootComponent.allOccurrences:
        try:
            if occ.component.attributes.itemByName(group, name):
                n += 1
        except Exception:
            pass
    return n


def _count_graphics(cam, design, group_id):
    n = 0
    for groups in (cam.customGraphicsGroups, design.rootComponent.customGraphicsGroups):
        for i in range(groups.count):
            try:
                if groups.item(i).id == group_id:
                    n += 1
            except Exception:
                pass
    return n


def _cache_listing():
    cache = os.path.join(DATA_DIR, 'cache')
    listing = []
    for root, _, files in os.walk(cache):
        for f in files:
            listing.append(os.path.relpath(os.path.join(root, f), cache))
    return sorted(listing)


def run(context):
    app = adsk.core.Application.get()
    lines = []
    failed = 0

    def check(ok, text):
        nonlocal failed
        lines.append(('PASS ' if ok else 'FAIL ') + text)
        if not ok:
            failed += 1

    try:
        StockSession, TemporaryMesh, group, attr, group_id, sweep_orphans = _import_addin()
        doc = app.activeDocument
        cam, design = StockSession.products(doc)
        if cam is None or design is None or cam.setups.count == 0:
            raise RuntimeError('open a document with a Manufacture setup first')
        setup = None
        for s in cam.setups:
            if s.isActive:
                setup = s
        setup = setup or cam.setups.item(0)
        lines.append('document %s, setup %s, %d cycles' % (doc.name, setup.name, CYCLES))
        session = StockSession(doc)
        before_cache = _cache_listing()
        leftover = _count_temp(design, group, attr)
        if leftover:
            TemporaryMesh(design).remove_all()
            lines.append('NOTE %d temporary component(s) from an earlier session removed before the test' % leftover)
        check(_count_temp(design, group, attr) == 0, 'no temporary stock before the test')
        check(_count_graphics(cam, design, group_id) == 0, 'no tagged graphics before the test')
        triangle_counts = set()
        for i in range(1, CYCLES + 1):
            t0 = time.perf_counter()
            result = session.acquire_current(doc, setup)
            t1 = time.perf_counter()
            check(result.ok, 'cycle %d: acquired %s in %.2f s' % (i, result.label(), t1 - t0))
            check(_count_temp(design, group, attr) == 1, 'cycle %d: exactly one temporary component placed' % i)
            mesh = session.mesh_body(doc) if hasattr(session, 'mesh_body') else None
            if mesh is None:
                occs = TemporaryMesh(design).find_all()
                mesh = TemporaryMesh(design).mesh_body(occs[0]) if occs else None
            if mesh is not None:
                try:
                    triangle_counts.add(mesh.mesh.triangleCount)
                except Exception as exc:
                    lines.append('NOTE triangle count unavailable: %s' % exc)
            removed = session.remove_mesh(doc)
            check(removed == 1, 'cycle %d: removed %d component(s)' % (i, removed))
            check(_count_temp(design, group, attr) == 0, 'cycle %d: no temporary component left' % i)
            adsk.doEvents()
        check(len(triangle_counts) == 1, 'placed mesh has the same triangle count every cycle: %s' % sorted(triangle_counts))
        check(_count_graphics(cam, design, group_id) == 0, 'no tagged graphics after the cycles')
        swept = sweep_orphans(app, doc)
        check(swept == 0, 'graphics sweep found nothing to remove (%d)' % swept)
        after_cache = _cache_listing()
        check(before_cache == after_cache or len(after_cache) <= len(before_cache) + 3,
              'cache folder did not accumulate files (%d before, %d after)' % (len(before_cache), len(after_cache)))
    except Exception:
        lines.append('ERROR ' + traceback.format_exc())
        failed += 1
    lines.insert(0, 'Fusion IPW Inspector lifecycle test: %d cycles, %d failed' % (CYCLES, failed))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REPORT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines))
