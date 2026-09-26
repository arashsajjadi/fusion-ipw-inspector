# Changelog

All notable changes to Fusion IPW Inspector are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-25

First release.

### Added
- `IPW Inspector` button in Manufacture > Inspect (next to Measure).
- Setup selector that defaults to the active Manufacturing Setup.
- Click a point on remaining stock, the model, a face, an edge or a point and read
  X, Y, Z relative to the selected Setup work coordinate system (WCS).
- Live viewport feedback: picked-point crosshair with coordinate label, and a WCS
  triad for the selected setup.
- `Copy XYZ` (tab separated) and `Copy as X.. Y.. Z..` (controller style).
- `Create reference point` adds a named construction point at the picked location.
- `Load saved stock...` imports the STL written by Simulation > Stock > Save Stock as a
  clearly named temporary mesh, with automatic unit (mm / cm / inch) and frame
  (world / any setup WCS) detection checked against the setup model.
- `Remove temporary stock`, automatic cleanup when the add-in stops.
- Document units by default, mm / inch override under Advanced.
- Optional diagnostics log, quiet by default.
- Pure-Python unit tests for the transform, formatting and stock-file inference.
