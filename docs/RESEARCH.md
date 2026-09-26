# How the in-process stock is acquired, and what else was tried

Fusion build 2.0.2705x (2705.1.15), Windows 11, September 2026. Everything below was
tried on this machine unless the row says otherwise. Research scripts lived in the
Fusion Scripts folder during the work and were removed afterwards; the reusable ones
are `tools/validate_in_fusion.py` and the helper post in `resources/post/`.

## The route that is used

Fusion's post-processing engine exports the setup stock and part as STL for any post
that sets `this.exportStock = true` / `this.exportPart = true`, and hands the file
paths to the post as the global parameters `autodeskcam:stock-path` and
`autodeskcam:part-path` at `onClose`. This is the documented mechanism the shipped
`camplete.cps` (machine simulation) post uses. For a setup whose stock is *From
preceding setup*, the exported stock is the in-process stock left by the preceding
setups, written in the setup's WCS in the document unit.

The add-in ships a tiny post (`ipw_inspector_stock.cps`) that emits no NC code and
copies the two files next to its output with a key=value sidecar. `stock/post_export.py`
installs that post into the personal post folder, creates a temporary NC program for
the setup through the public `NCPrograms` API, posts it into the add-in's cache
folder, reads the sidecar, deletes the NC program again (one undo step) and imports
the STL as the temporary component. Measured: 0.2 to 0.4 s per export, 1.5 s to import
829 000 triangles, 2.3 s to delete them again.

Two facts about Fusion that this depends on, both established experimentally:

- Fusion computes the incoming stock of a *From preceding setup* setup when that
  setup's operations are generated, and only if the setup carries the
  `job_continueMachining` flag that the Setup dialog sets together with the stock
  mode. Setups created through the API with `stockMode = PreviousSetupStock` alone do
  not get the flag and export the raw box; the add-in reports this instead of changing
  the setup.
- The export reads Fusion's own in-process-stock result; simulation does not need to
  have run.

## Approaches investigated

Legend for *Result*: **used** (in the product), **fallback** (kept, secondary), **no**
(does not give the stock or was rejected), **n/a** (assessed by reasoning, not run).

| # | Approach | Result | Why accepted / rejected | Stability | Maintenance risk |
|---|----------|--------|-------------------------|-----------|------------------|
| 1 | Public `adsk.cam` stubs of this build, grep for stock / IPW / simulation / mesh | no | `Setup.stockSolids` (only *From solid*), `stockMode`, `workCoordinateSystem`, parameters; no stock geometry accessor | | |
| 2 | `dir()` / runtime introspection of `CAM`, `Setup`, `Operation`, `CAMManager` | no | No undocumented members beyond the stubs | | |
| 3 | `OperationBase.generatedDataCollection` / `GeneratedDataType` | no | Additive results only (`OptimizedOrientation`, `AdditiveFEA`, `InterferenceAnalysis`) | | |
| 4 | Setup / operation parameters (`stockXLow`… `job_stockMode`…) | partial | Box dimensions and modes only, used for validation; no mesh | documented | low |
| 5 | Hidden Neutron Python modules (`Python.ListModules /Hidden`: `neu_server`, `neu_modeling`, `neu_grx`, `fusion_server`) | no | `get_body_triangles` works for BRep bodies only; nothing for CAM stock | undocumented | |
| 6 | Scene graph (`PScene.GetModelScene`, `get_node_from_entity`) | no | Nodes are reachable from entities; the in-process stock is an adorner, not an entity | undocumented | |
| 7 | Text command inventory (`TextCommands.List`, 5 766 commands) | no | No stock export command; gave the diagnostics used below | | |
| 8 | Command definitions before / during / after Simulation (`DebugCommands.ListCommandDefinitions`, `AllApplicableCommands`, `RunningCommandInfo`) | no | Save Stock has **no** command definition; it is a Qt context-menu action created on demand | | |
| 9 | Qt object dump during Simulation (`Toolkit.DumpQt`) | no | No stock menu widgets exist until the menu is opened | | |
| 10 | Simulation toolbar dump (`Toolkit.toolbars /items`) | no | Only display controls (`stock_accuracy`, `show_stock_transparent`…) | | |
| 11 | `Commands.ContextMenu <Keyevent>` to open the viewport menu from a script | no | Every argument form fails with "bad lexical cast" | | |
| 12 | `UI.SaveDialogMock` | no | Opens the cloud Save dialog, unrelated | | |
| 13 | `IronGenerateStock` ("Generate Stock") | no | Disabled and hidden for setups and operations in this build; `execute()` is a no-op | | |
| 14 | `IronInProcessStockDisplay` (Display in-process stock, F8) | no | Displays the IPS adorner; nothing exported or selectable | | |
| 15 | `IronAutomaticIPSGeneration` (option `InProcessStockGenerationPaused`) | no | Controls background generation only; does not create the preceding-setup transfer | | |
| 16 | `IronClearIPSCache` | rejected | Destructive, confirmation dialog | | |
| 17 | `SimulationStockToModel` ("Stock to Model") | no | Only enabled inside an interactive Simulation session, which ends when a script runs | | |
| 18 | `Selection.point` on the displayed IPS | no | Adorners are not selectable entities | | |
| 19 | Fusion options (`Options.List`, 27 stock/simulation options) | no | Generation modes and display flags only | | |
| 20 | Binary string analysis of `NaNeuCAMUI10.dll`, `IronCore10.dll`, `mwVerifier.dll`, `post.exe` | lead | Found `saveStock@CommonSimulationManager` (C++ only), `ExportStockMesh` (MachineWorks), and the `stock_%1.stl` / `:stock-path` strings that pointed at the post engine | | |
| 21 | C++ add-in API headers (`API/CPP/include/Cam`) | no | Same object model as Python; no stock accessor | | |
| 22 | TypeScript API (`TypeScript.APIEnabled`) | n/a | Same object model; not exercised | | |
| 23 | File-system watch of `%LOCALAPPDATA%\Temp\Fusion360CAM\<pid>-N` during posting | lead | Found `stock_1.stl` / `part_1.stl` written by the post engine and per-operation `.xbsrf` job files | | |
| 24 | Fusion's IPS cache `%LOCALAPPDATA%\Temp\Fusion360CAM\meshes\<guid>.json + .xbsrf` | no (documented) | Readable: chunked file, `UNIT=MM`, `CFAC` blocks of float32 vertices + uint16 triangles in world space. The descriptor carries no document or operation id (`inputDigest` all zero), so a mesh cannot be matched to a setup safely | undocumented | high |
| 25 | Hub cloud cache (`Autodesk Fusion 360\<hub id>`) | no | Only crash-recovery backups change during machining | | |
| 26 | Open-file inspection of the Fusion process | n/a | Unnecessary once the folders above were found | | |
| 27 | **Post engine `this.exportStock` + `autodeskcam:stock-path`** | **used** | Documented post feature; 0.3 s; exact in-process stock in the setup WCS | documented | low |
| 28 | `NCPrograms.createInput` / `postConfiguration` by `user://` URL / `nc_program_output_folder` expression / `postProcess` | used | Public API; folder must be set through `expression`, `value.value` is ignored | documented | low |
| 29 | Legacy `CAM.postProcess(PostProcessInput)` | not used | Also available; the NC-program route is the current one | documented | |
| 30 | Simulation of the preceding setup as a trigger | no | Simulating to the end changes nothing for the export | | |
| 31 | Regenerating the following setup's operations | conditional | Makes Fusion compute the incoming stock, but only with `job_continueMachining` (see above) | documented | low |
| 32 | `job_continueMachining` parameter diff between UI-created and API-created setups | key finding | Explains raw-box exports; the add-in reports it, never sets it on user setups | | |
| 33 | Reconstructing the IPW from toolpaths (stock minus swept tool volumes) | rejected | Unnecessary once 27 worked; would duplicate MachineWorks with worse accuracy | | |
| 34 | Save Stock through UI automation (SendInput / accessibility) | rejected | Brittle, steals the mouse, cannot run while a script runs; the human step is kept only as the last fallback | | |
| 35 | Folder watcher for a manually saved stock (`FindFirstChangeNotification`, no polling) | fallback | Picks up a Save Stock file automatically while the dialog is open | Win32, documented | low |
| 36 | Manual file pick | fallback | Last resort under Advanced | | |
| 37 | Graphics / GPU buffer readback of the rendered stock | n/a | No API; would be rendering reverse-engineering | | |
| 38 | Named pipes, sockets, COM, .NET interop into CAM | n/a | No public surface; not pursued after 27 succeeded | | |
| 39 | Undo stack / document serialisation for stock data | n/a | Fusion stores IPS as external cache files (row 24), not in an inspectable document stream | | |
| 40 | Task manager (`IronTaskManager`) as a progress hook | no | Shows no IPS tasks in this build | | |

Forty avenues were investigated with evidence; the requested one hundred was not
reached because the post-engine route (row 27) turned out to be exact, fast and
documented, and the remaining ideas were variations of rejected ones.

## Cache file format (for the record)

`.xbsrf` ("extensible binary surface", written by `Fusion CAM 2705.1.15`):
a sequence of chunks `tag(4) size(4) payload`. `TEXT` chunks hold `len(4)` + string
(`VERSION=1`, `UNIT=MM`, `CLOSED=…`, `TOLERANCE=0.01`). `CFAC` chunks hold
`nVertices(4) nTriangles(4)`, then `nVertices × 3 float32`, then `nTriangles × 3 uint16`
indices. `ENDM` ends the file. Coordinates are world millimetres.
