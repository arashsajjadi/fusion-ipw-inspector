# Changelog

All notable changes to Fusion IPW Inspector are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

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
