# Changelog

All notable changes to Fusion IPW Inspector are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-09-26

### Fixed
- Fusion still tinted the whole stock overlay while the cursor was over it: the pick input's
  `MeshBodies` filter made Fusion pre-highlight the body even though it was not selectable. With
  model selection off the input now uses a filter that never matches the stock, so the overlay
  keeps its normal shading while hovering and Fusion does no per-move work on the 829 000-triangle
  body at all.

## [0.4.0] - 2026-09-26

Interaction made instantaneous and smooth on the real 829 000-triangle Setup5 stock. The
acquisition, WCS transform and export core are unchanged; all 0.3.0 accuracy tests still pass.

### Added
- Precomputed feature graph per stock (`core/features.py`): planar patches, edges and corners
  extracted once in the background (4.25 s), stored in coarse cells and cached next to the
  exported stock (`.snapcache`, loads in 0.16 s). A hover now looks up cached candidates instead
  of reconstructing planes from triangles; the local reconstruction remains as the fallback while
  the graph is building. Both corners of one face share the same fitted planes.
- Own ray cast on the analysis mesh (grid traversal, 0.03 ms): hovering and clicking go through
  `mouseMove`/`mouseUp`, the display mesh is non-selectable and Fusion no longer hit-tests or
  highlights the 829 000-triangle body.
- Magnetic tracker: features acquired within the tolerance are held until two tolerances away;
  higher-priority features take over within the tolerance; no alternation on tiny motion.
- **Show IPW** checkbox: hides/shows the stock overlay in a few milliseconds without touching the
  snap data or the model.
- Per-stage hover profile in the diagnostics log on close; startup timings (export, import,
  dialog, index, features, cache).
- 27 new unit tests (feature graph, ray cast, plain-data cache state, magnetic tracker).

### Changed
- The hover marker is a set of persistent custom-graphics groups moved through their transforms;
  nothing is deleted or recreated per hover, and viewport repaints are limited to 60 Hz.
- Per-hover cost on the watch case: 17.7 ms → 2.3 ms median (7.6 ms p95 including the repaint);
  the pipeline sustains 170 events per second instead of saturating at 56.
- The stock fingerprint ignores the STL attribute bytes Fusion leaves undefined, and the cache
  file survives re-exports, so unchanged stock reuses its index and features across sessions.
- Status line says *Preparing IPW…* while the index is still being built.

### Fixed
- The screen tolerance was quantised by integer pixel coordinates (it flipped between 0.71 and
  0.75 mm); it is now measured over a 10 mm step.

## [0.3.0] - 2026-09-26

Product-quality pass on interaction, lifecycle and documentation. The validated
acquisition, WCS transform and export core of 0.2.0 are unchanged.

### Added
- Geometric snapping on the in-process stock: hovering previews a **CORNER**
  (three-plane intersection), **EDGE** (two-plane intersection) or **SURFACE**
  (plane fit) reconstructed from the triangles around the cursor; a click picks the
  previewed feature. Screen-space tolerance (12 px), candidate ranking, hysteresis,
  and **N** to cycle candidates. Internal STL triangulation edges are never edges.
- Pure-Python spatial index (`core/mesh_index.py`, uniform grid, float32, chunked
  build) and reconstruction core (`core/snap.py`); the index is built once per stock
  in a background thread and cached per stock fingerprint.
- Feature type and real fit metrics in the result: method, RMS residual, conditioning,
  distance from the cursor hit, and a confidence label derived from them.
- Lightweight confirmations: "XYZ copied: …" / "G-code copied: …" in the status line.
- Viewport markers by shape and label (cross, line, diamond, dot), cyan for the
  preview and orange for the pick; sizes follow the model size.
- Automatic lifecycle: closing, cancelling, reloading the add-in or switching
  documents removes the temporary stock and every custom graphics group; leftover
  graphics are tagged and swept once the dialog has fully closed.
- `tools/lifecycle_test.py` (in-Fusion acquire/remove cycles with cleanup counts),
  19 snapping unit tests (52 in total), a demo GIF and CORNER/EDGE screenshots,
  `docs/SNAPPING.md`, `docs/DEVELOPMENT_HISTORY.md`.
- The optional installer reports the installed version.

### Changed
- The panel is shorter and simpler: Setup, one status line
  (`● Current IPW · Setup5 · Ready`, `Preparing snapping…`, `⚠ No IPW available: …`),
  the pick control, the result block (feature, X/Y/Z, method · residual · confidence),
  Copy XYZ, Copy G-code. Everything else is under Advanced (units, WCS triad, allow
  model selection, reference point, refresh, import saved stock, diagnostics, self test,
  WCS details and snap statistics).
- The stock is drawn without triangle edges and slightly translucent so it reads as an
  overlay, distinct from model geometry.
- README rewritten for first-time users (install in two minutes, update, uninstall).

### Fixed
- A blank transient window ("IPW Inspector", 269 × 147 px) flashed during acquisition:
  it was a progress dialog and is gone. 10 monitored launches show no transient window.
- The WCS triad was drawn again on every redraw and its groups outlived the dialog,
  which made axis labels look doubled after a few sessions.
- Hover previews requested through `executePreview` cancelled the click that followed;
  the preview is now drawn directly from the hover event.
- Custom graphics under the cursor could intercept the click; all markers are
  non-selectable.
- A manually cycled candidate was overridden by the automatic ranking on the hover
  that precedes a click.

### Removed
- The *Remove IPW mesh* button (cleanup is automatic).

## [0.2.0] - 2026-09-25

The manual Save Stock workflow is gone: clicking **IPW Inspector** fetches the
current in-process stock of the setup by itself.

### Added
- Automatic in-process stock acquisition through Fusion's post-processing engine
  (`stock/post_export.py`, helper post `resources/post/ipw_inspector_stock.cps`).
  One click, no file dialogs, about 0.3 s to export plus 1.5 s to import a
  830 000-triangle stock; unchanged stock is reused without re-import.
- Provider chain with a common result type: post export (current IPW), then a saved
  file (auto-detected in the last Save Stock folder while the dialog is open, or
  picked manually).
- Source badge in the dialog: **Current IPW**, **Saved IPW** or **No IPW available**,
  and each reading says `on IPW` or `on model`.
- Mesh validation before a stock is trusted: scale, containment, orientation against
  the exported part, empty meshes, unmachined boxes.
- **IPW Inspector Self Test** hidden command and *Run self test* button: transform
  math, unit conversion, WCS validity, stock scale, orientation and containment.
- Analytic in-Fusion validation script `tools/validate_in_fusion.py` (17 checks on a
  synthetic pocket job).
- Temporary stock is removed automatically before the document is saved and when
  the add-in stops; every add-in component is tagged so nothing else is touched.
- With an IPW loaded, picks are restricted to the IPW (Fusion prefers solids over
  meshes under the cursor); *Also pick model geometry* opts back in.

### Changed
- Compact dialog: Setup, source line, Point, X/Y/Z, Copy XYZ, Copy G-code. Everything
  else lives under Advanced; the WCS description moved there.
- Reference points no longer switch workspaces: they are anchored to a sketch point
  in parametric designs, placed directly in direct-modelling designs.
- Package layout: `core/` (transform, units, STL), `stock/` (providers, temporary
  mesh, validation), `ui/`, `diagnostics/`, `commands/`.

### Removed
- The "Load saved stock" step as the primary workflow. It remains as a fallback under
  Advanced.

## [0.1.0] - 2026-09-25

First release: Setup WCS transform, point inspection, copy formats, reference point,
manual saved-stock import.
