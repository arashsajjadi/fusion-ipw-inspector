# Snapping on the in-process stock

How a hover over the IPW mesh becomes a CORNER, EDGE or SURFACE candidate, what is rejected,
how the result is validated, and what it costs. Everything here is pure Python
(`core/mesh_index.py`, `core/snap.py`); Fusion only supplies the hover hit point and the camera.

## Pipeline per hover

1. **Hit.** Fusion's `preSelectMouseMove` gives the world point under the cursor on the temporary
   stock mesh. It is transformed into the mesh (setup) frame with the setup WCS.
2. **Radius.** A 12 px screen tolerance is converted to millimetres at the depth of the hit by
   projecting the hit and a point 1 mm to its right through `Viewport.modelToViewSpace`
   (`snap_controller.radius_mm`). Clamped to 0.02–5 mm.
3. **Neighbourhood.** `MeshIndex.triangles_near(hit, radius)` returns the triangles whose cells
   intersect the sphere (bounded to 4000 triangles).
4. **Planes.** Triangle normals are clustered greedily (12° cone, 0.05 mm offset). Each cluster is a
   candidate plane with an area-weighted normal, an RMS residual of its vertices and a support
   count. Clusters with less than 3 % of the local area are dropped.
5. **Features.**
   - CORNER: every triple of planes whose normal matrix has |det| ≥ 0.15 is intersected; the point
     must lie within 1.5 × radius of the hit and within reach of each plane's support.
   - EDGE: every pair of planes at ≥ 20° is intersected into a line; the hit is projected onto it,
     with the same reach checks.
   - SURFACE: the hit projected onto each plane.
   - RAW: the hit itself, always present.
6. **Ranking.** CORNER > EDGE > SURFACE > RAW, then by distance from the hit.
7. **Hysteresis and cycling.** `SnapTracker` keeps the current candidate while an equivalent one
   is still available (same kind, same location within half the radius, same direction for edges,
   same plane for surfaces). It switches when the feature disappears or a higher-priority one is
   clearly closer. **N** cycles through the list; a cycled choice is sticky until the cursor leaves
   the feature (the hover Fusion fires right before a click would otherwise revert it).
8. **Commit.** A click uses the previewed candidate if it is still within reach of the click
   point, otherwise it snaps afresh. The candidate is transformed back to world coordinates and
   then into the selected setup's WCS like any other pick.

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

## Evidence on the watch case (Setup5, 828 912 triangles)

Expected values were derived independently from the stock STL that Fusion exports for the setup:
axis-aligned planar patches were extracted with NumPy (normal ±x/±y/±z, connected by shared
vertices) and their extents intersected. The tab block at the +y/+z end of the frame is bounded by
the planes x = 2.0776, y = 25.150, z = 22.000 (top) and z = 17.920 (underside of the rail).

| Feature | Expected | Measured (installed 0.3.0) | Error |
|---------|----------|----------------------------|-------|
| corner (2.0776, 25.150, 22.000), cursor 0.56 mm away | exact | (2.0776, 25.1500, 22.0000), residual 0.0000, cond 1.00 | 0.0000 mm |
| corner (2.0776, −25.150, 22.000), cursor 0.48 mm away | exact | (2.0776, −25.1500, 22.0000), residual 0.0000, cond 1.00 | 0.0000 mm |
| corner (2.0776, 25.150, 17.920), cursor 0.40 mm away | exact | (2.0776, 25.1500, 17.9212), residual 0.0003, cond 1.00 | 0.0012 mm (tessellation of the underside: z 17.919–17.921 in the file) |
| edge x = 2.0776, z = 22 | line | (2.0776, 19.9322, 22.0000), residual 0.0000 | 0.0000 off the line |
| face z = 22 | plane | (0.1661, 19.9322, 22.0000), residual 0.0000 | 0.0000 off the plane |

Screenshots: `images/corner_snap.png`, `images/edge_snap.png`; the demo GIF shows the full flow.

## Complexity and cost

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
