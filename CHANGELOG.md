# Changelog

All notable changes to Fusion IPW Inspector are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

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
