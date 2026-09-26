# Snapping on the in-process stock

How a mouse position over the IPW becomes a CORNER, EDGE or SURFACE candidate, what is rejected,
how the result is validated, and what it costs. Everything here is pure Python
(`core/mesh_index.py`, `core/features.py`, `core/snap.py`); Fusion only supplies the mouse
position and the camera. The full-resolution exported STL is the only source of coordinates; the
mesh Fusion draws is display only.

## Once per stock (background thread, cached)

1. **Index.** The STL is read into one `array('f')` and a uniform grid is built over the
   triangles (about 16 per cell; 0.41 mm cells on the watch case). 1.27 s for 828 912 triangles.
2. **Feature graph** (`FeatureGraph.build`, 4.25 s in Fusion's Python):
   - normals and areas of all triangles (one pass);
   - per grid cell, triangles are clustered into cell planes (10°, 0.05 mm);
   - cell planes of neighbouring cells that describe the same plane are merged with a union-find
     into patches (4°, 0.06 mm mutual offset); a cell plane that drifted more than 6° / 0.1 mm from
     its patch's plane (curved surfaces) is split off; every patch gets an area-weighted plane from
     its unique triangles and an RMS residual over all their vertices;
   - patches present in neighbouring cells are adjacent; adjacent good patches (residual ≤ 0.05 mm,
     each at least three cells wide across the edge) at ≥ 20° give edges, clipped to the runs of
     cells where both faces really meet;
   - an edge plus a third patch adjacent to both gives a corner when |det| of the unit normals is
     ≥ 0.15 and the point lies on the edge run where all three patches are present.
   Corners, edge segments and patches are registered in coarse cells (about 3 mm) for O(1) lookup.
3. **Cache.** Index and graph are written as plain data next to the exported stock
   (`<stock>.stl.snapcache`, 51 MB, 0.2 s to write, 0.16 s to load). The stock fingerprint ignores
   the STL attribute bytes Fusion leaves undefined, so an unchanged stock keeps its cache across
   exports, dialog sessions and Fusion restarts. A stale cache (different stock or layout version)
   is rebuilt.

## Per hover (main thread, 2.3 ms median including the marker)

1. **Ray.** `Command.mouseMove` gives the viewport position; `Viewport.viewToModelSpace` and the
   camera eye/target give a ray, expressed in the mesh frame through the setup WCS (0.08 ms).
2. **Cast.** Amanatides–Woo traversal of the grid; only the triangles of the cells the ray crosses
   are tested, front to back, stopping at the first hit (0.03 ms median).
3. **Tolerance.** 12 px converted to millimetres at the hit depth by projecting a 10 mm step
   through `modelToViewSpace` (0.12 ms). Clamped to 0.02–5 mm.
4. **Query.** The coarse cells within two tolerances of the hit give the cached corners (point
   distance), edge segments (closest point on the segment) and patches (projection; the patch under
   the hit triangle always counts) (0.07 ms median, 0.45 ms p95). While the graph is still building,
   the 0.3.0 local reconstruction (`snap.snap`) is used with geometric keys so the tracker behaves
   the same.
5. **Magnetic tracker** (`MagneticTracker`): candidates are ordered CORNER > EDGE > SURFACE > RAW,
   then by distance. A feature is acquired within the tolerance R and retained while the same
   feature (by id) is within 2 R. A higher-priority feature within R takes over; a same-priority
   feature only when it is within R and closer by at least 0.5 R. **N** cycles through the
   candidates; the choice sticks while the feature stays within reach.
6. **Marker.** One persistent custom-graphics group per feature kind plus a label group; a hover
   only sets the group's transform (position, size, edge direction), toggles visibility between
   kinds and updates the label text when it changes (1.9 ms, mostly Fusion API calls). Fusion does
   not repaint custom graphics by itself, so a repaint is requested at most 60 times per second;
   moves inside the same frame schedule one deferred repaint.
7. **Click.** `mouseDown`/`mouseUp` without drag commits the previewed candidate from an idle
   event, after Fusion finished its own selection handling; when *Allow model selection* is on and
   Fusion selected model geometry for the same click, that pick wins.

## Why internal triangulation edges never appear

Edges are only ever produced by intersecting two *planes*, and a plane is a cluster of coplanar
triangles. The diagonal between two triangles of the same flat face lies inside one cluster, so
there is no second plane to intersect. Unit test:
`test_internal_triangulation_edges_are_not_edges`.

## Rejections

| Case | Rule | Test |
|------|------|------|
| near-parallel planes | angle < 20° gives no edge | `test_near_parallel_planes_do_not_make_an_edge` |
| ill-conditioned corner | \|det(n1, n2, n3)\| < 0.15 | `test_unstable_three_plane_intersection_is_rejected` |
| far corner | farther than 1.5 × radius from the hit | `test_corner_far_from_hit_is_rejected` |
| unsupported feature | intersection farther than 1.5 × radius from a plane's supporting triangles | `test_edge_requires_support_from_both_planes` |
| noisy plane | residual > 0.05 mm drops the plane | `test_noisy_plane_residual_is_reported` |

## Confidence

`Candidate.confidence()` maps geometric metrics to a label; no invented percentages:

- **high**: RMS residual ≤ 0.01 mm and conditioning ≥ 0.5 (conditioning is |det| of the unit
  normals for a corner, |sin| of the angle for an edge, 1 for a surface);
- **medium**: residual ≤ 0.05 mm and conditioning ≥ 0.25;
- **low**: anything that was accepted but is worse than that;
- **raw surface hit**: the RAW candidate.

Advanced shows the raw numbers: method, residual, conditioning, distance from the hit, and the
index statistics.

## Measured on the watch case (Setup5, 828 912 triangles), 0.3.0 vs 0.4.0

| Stage per hover | 0.3.0 | 0.4.0 |
|-----------------|-------|-------|
| entity check / ray + cast | 0.14 ms | 0.08 + 0.03 ms |
| screen tolerance | 0.15 ms | 0.12 ms |
| geometric query | 4.59 ms (local reconstruction) | 0.07 ms (cached features) |
| tracker | 0.01 ms | 0.02 ms |
| marker update | 5.70 ms (delete + recreate) | 1.85 ms (transform + label) |
| viewport repaint | 7.20 ms every event | 0 ms median, 5–7 ms at most 60×/s |
| total per event | 17.7 ms median, 19.5 ms p95 | 2.3 ms median, 7.6 ms p95 |
| events per second sustained | 56 (saturated by the pipeline) | 170 (limited by the mouse) |

Cold open: export 0.33 s, display import 2.1–2.3 s, dialog 0.015 s, then index 1.27 s and
features 4.25 s in the background (snapping falls back to local reconstruction meanwhile). Warm
open: the same export and import, features from memory (same session) or from the cache file in
0.16 s. Remaining O(N) work: STL read, grid build and feature extraction (once per stock, cached),
and Fusion's own import of the display mesh.

## Evidence on the watch case (Setup5, 828 912 triangles)

Expected values were derived independently from the stock STL that Fusion exports for the setup:
axis-aligned planar patches were extracted with NumPy (normal ±x/±y/±z, connected by shared
vertices) and their extents intersected. The tab block at the +y/+z end of the frame is bounded by
the planes x = 2.0776, y = 25.150, z = 22.000 (top) and z = 17.920 (underside of the rail).

| Feature | Expected | Measured (installed 0.3.0) | Error |
|---------|----------|----------------------------|-------|
| corner A (2.0776, 25.150, 22.000), cursor 0.57 mm away | exact | (2.0774, 25.1500, 22.0000), residual 0.0004, cond 1.00 | 0.0002 mm |
| corner B (2.0776, −25.150, 22.000), cursor 0.63 mm away | exact | (2.0773, −25.1500, 22.0000), residual 0.0003 | 0.0003 mm |
| corner C (−3.472, 25.150, 22.000), cursor 0.65 mm away | exact | (−3.4727, 25.1500, 22.0000), residual 0.0008 | 0.0007 mm |
| corner D (2.0776, 25.150, 17.920), cursor 0.52 mm away | exact | (2.0777, 25.1500, 17.9243), residual 0.0036 | 0.0043 mm (the underside plane fitted over its whole face: z 17.919–17.921 in the file) |
| edge x = 2.0776, z = 22 | line | (2.0774, 20.0036, 22.0000), residual 0.0004 | 0.0002 off the line |
| face z = 22 | plane | (−0.4813, 20.0036, 22.0000), residual 0.0000 | 0.0000 off the plane |
| corners A and C (same tab, same top and side faces) | shared y and z | y 25.1500 / 25.1500, z 22.0000 / 22.0000 | identical to 1e-5 mm (same fitted patches) |

The 0.3.0 values (local reconstruction) are in the Git history of this file; the 0.4.0 corners
come from planes fitted over whole faces, so A/B/C share the x = 2.0774 face plane exactly.

Magnetic behaviour recorded on the installed build (1 px cursor steps along the tab's top edge
towards corner A, past it and back, tolerance R = 0.71–0.75 mm): EDGE held for 27 steps
(distance 0.36–0.44 mm), CORNER acquired at 0.70 mm, held while the cursor moved away to 1.35 mm
(< 2 R), SURFACE on the side face, EDGE on the way back, CORNER acquired at 0.63 mm and held to
1.47 mm, then EDGE again. No alternation at any step.

Screenshots: `images/corner_snap.png`, `images/edge_snap.png`; the demo GIF shows the full flow.

## Complexity and cost (index; feature graph above)

- **Index build**: one pass over the file (`array('f')` of coordinates, chunks of 65 536
  triangles), one pass to assign each triangle to the cells its bounding box covers. O(N) once per
  stock, in a background thread; 1.27 s for 828 912 triangles inside Fusion's Python, 0.72 s of
  which is the grid. Peak additional memory +156 MB while building, +79 MB retained (float32
  coordinates 30 MB, integer cell lists, cached normals and areas). Cell size is chosen for about
  16 triangles per cell (0.41 mm here, 47 063 occupied cells, 33 triangles per cell on average,
  167 maximum).
- **Hover**: cells within the radius (typically 3³ to 5³), their triangles (tens to a few hundred),
  clustering O(k · c) with k triangles and c clusters (c is small), intersections O(c³) with c ≤ 6.
  Measured 2.1 ms median, 4.1 ms p95 over 403 hovers in Fusion; 4.2 ms / 6.3 ms offline with the
  same stock. No per-hover work touches the whole mesh.
- **Remaining O(N)**: the initial STL read and the index build (unavoidable; done once, cached per
  stock fingerprint across dialog sessions), and Fusion's own mesh import (2.0–2.4 s for this
  stock, inside Fusion). `_pick_cell_size` reads the bounding box in the same pass as the load.
- **Memory choices**: coordinates live in one `array('f')`; per-cell lists hold triangle indices;
  normals and areas are computed lazily and cached only for triangles that were visited.
- Larger meshes scale linearly in build time and memory; hover cost depends only on local density.
  A finer stock (smaller triangles) raises the per-cell count, which the auto cell size compensates.

## Not implemented (yet)

Cylinder and circle fitting. Curved features currently give SURFACE (when locally flat enough to
cluster) or RAW hits; the residual in Advanced shows when a plane fit is a poor description.
