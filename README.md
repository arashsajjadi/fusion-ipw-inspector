# Fusion IPW Inspector

Click a point on the in-process stock of any Manufacturing Setup and read its X, Y, Z in that
setup's work coordinate system. One button, no export dialogs.

![IPW Inspector dialog](docs/images/dialog.png)

1. Open the Manufacture workspace and click **IPW Inspector** (Inspect panel, next to Measure).
2. The in-process stock of the active setup appears in the viewport. Click a point on it.
3. Read X, Y, Z. They are relative to the WCS of the setup in the dropdown.

That is the whole workflow. Copy the values, copy them as `X.. Y.. Z..`, or drop a reference
point. Switching the setup re-reads the stock for that setup and re-expresses the picked
point in its WCS.

![Picked point on a leftover tab of a rotary setup](docs/images/marker.png)

## Install

1. Download the ZIP from the [releases page](https://github.com/arashsajjadi/fusion-ipw-inspector/releases) (or clone).
2. Copy the `FusionIPWInspector` folder into the Fusion add-ins folder:

   | OS      | Folder |
   |---------|--------|
   | Windows | `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\` |
   | macOS   | `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/` |

   The result must be `...\AddIns\FusionIPWInspector\FusionIPWInspector.py`.
3. Restart Fusion, or open **Utilities > Add-Ins > Scripts and Add-Ins**, select *FusionIPWInspector*, click **Run**.
4. Open **Manufacture**. The button is in the **Inspect** panel of every Manufacture tab.

Windows users can run the optional installer from the extracted folder instead (no administrator rights):

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1 -Uninstall` removes it. No Python packages, no network access, no admin rights.

## What you see

- **Setup** – which WCS the numbers are in. Defaults to the active setup, else the selected one.
- **Current IPW ✓ Setup5** – the source line. It is one of *Current IPW* (read from Fusion just
  now), *Saved IPW* (a file you saved from Simulation) or *No IPW available* (with the reason).
- **X / Y / Z** and, below them, `on IPW` or `on model` so you always know what you clicked.
  Readings on the IPW carry the simulation-resolution note; readings on model geometry are exact.
- **Copy XYZ** copies `12.384<tab>-21.750<tab>6.125`; **Copy G-code** copies `X12.384 Y-21.750 Z6.125`.
- **Advanced** – display unit (document unit, mm or inch), WCS triad, *Also pick model
  geometry*, *Create reference point*, *Refresh IPW*, *Load saved stock…*, *Remove IPW mesh*,
  the WCS description, the diagnostics log switch and *Run self test*.

Units follow the document (3 decimals in mm, 4 in inches), with an explicit sign and never `-0.000`.

While an IPW is loaded only the IPW can be picked: Fusion prefers solid bodies over meshes when
both are under the cursor, and a modelled tab 0.05 mm below the real stock would otherwise be
reported as the stock. Tick *Also pick model geometry* when you want a model reference.

## How the in-process stock gets there

Fusion still has no public API call that returns the in-process stock (checked against build
2.0.2705x; the full investigation with 40 avenues is in [docs/RESEARCH.md](docs/RESEARCH.md)).
It does, however, export the setup stock and the part as STL for any post processor that asks
for them (`this.exportStock = true`), the same documented mechanism the shipped CAMplete
machine-simulation post relies on. For a *From preceding setup* setup that stock **is** the
in-process stock.

So the add-in ships a tiny post (`resources/post/ipw_inspector_stock.cps`) that writes no NC
code. When you click the button it installs that post into your personal post folder if it is
missing, creates a temporary NC program for the setup, posts it into the add-in's cache folder,
reads the exported stock, deletes the NC program again (all of that is a single undo step), and
imports the stock as a component named **"IPW Inspector temporary stock (not saved, safe to
delete)"** placed with the setup's WCS. The export takes about 0.3 s; importing an 830 000-triangle
stock takes 1.5 s. Unchanged stock is reused without re-import. The temporary component is removed
before the document is saved, when the add-in stops, or with *Remove IPW mesh*.

Two things Fusion requires, both reported in the dialog when they are missing:

- the setup must contain at least one generated operation (Fusion computes the incoming stock
  of a *From preceding setup* setup when its operations are generated);
- setups created through the API with `stockMode = PreviousSetupStock` alone lack the
  `job_continueMachining` flag the Setup dialog sets, and export the raw box. Re-select *From
  preceding setup* in the Setup dialog to fix such a setup. Setups made in the UI are fine.

Fallbacks, in order: a stock file that appears in your last Save Stock folder while the dialog is
open (Windows change notification, no polling), or *Load saved stock…* under Advanced. Files are
checked for unit, containment of the model and scale before they are used.

## What the numbers mean

`Setup.workCoordinateSystem` is the setup-to-world matrix; its translation is in millimetres
regardless of document units (verified by switching a test document to inches). The picked point
comes from Fusion's viewport selection in world space. The add-in applies

```
p_setup = Rᵀ · (p_world − origin)
```

after validating that the matrix is a proper right-handed rigid transform (a scaled or mirrored
matrix is refused with a message). Machine coordinates, tool orientation and rotary axes are not
involved: for a rotary setup the WCS is simply a WCS whose X (or Y) axis happens to be the rotary
axis, and the readings are what the post outputs relative to.

## Verified results

Independent checks, all on this Fusion build:

**Analytic job** (`tools/validate_in_fusion.py`, 17 checks, all pass): a 40 x 30 x 20 mm block with
a 20 x 10 x 8 mm pocket, Setup 1 clears it with a 4 mm end mill, Setup 2 uses the preceding stock
with its WCS at the stock's top corner (−1, −1, 21). The exported IPW has the pocket floor at
z = 12.000 (analytic 20 − 8), the outline at the model box (x 0..40, y 0..30, z 0..20.035), the
exported part matches the model box within 0.0000 mm, the placed mesh's transform equals the
setup WCS exactly, and the world→setup→world round trip is exact.

**Real watch-case job** (Setup3 fixed box, Setup2 flipped 180° about X, Setup5 rotary with X along
world +Z). Readings are compared with Fusion's own stock file for the setup, which Fusion writes
in the setup WCS without any add-in transform involved:

| Pick | Displayed (Setup5 WCS, mm) | Fusion's stock file | Agreement |
|------|----------------------------|---------------------|-----------|
| top of a leftover tab | +2.078 / −19.235 / +18.988 | tab plane X = 2.0776 | exact on the plane |
| island top | +10.828 / −1.058 / +0.811 | island plane X = 10.8276 | exact on the plane |
| stock side face | −0.002 / +4.818 / +15.143 | vertex (−0.0019, 4.8178, 15.1427) | 0.000 mm |
| stock end face | −2.944 / −23.587 / −22.000 | end plane Z = −22.0 | exact on the plane |
| island top (model geometry) | +10.778 / −0.684 / −0.040 | world z = 10.000 → X = 10.7776 | exact |
| same pick in Setup2 | −22.040 / +24.466 / −23.378 | hand computation | exact |
| same pick in Setup3 | −22.040 / +25.834 / −1.822 | hand computation | exact |

The stock file's bounding box in the Setup5 frame is x −12.600..10.828, y −25.150..25.157,
z −22.000..22.000, so the tabs (X ≈ 2.08) and the island (X ≈ 10.83) are where a machinist would
touch off: 10.83 mm above the raw stock centre along the rotary axis, 20 mm out radially.

**Pure-Python unit tests** (32, run anywhere):

```bash
python -m unittest discover -s FusionIPWInspector/tests -v
```

**Self test** inside Fusion (Advanced > Run self test, or the hidden command *IPW Inspector Self
Test*): transform cases A–D, unit conversion, rejection of bad matrices, every setup's WCS, and
for the loaded stock: readable, scale plausible, encloses the model, orientation of the exported
part within 0.007 mm, fits the setup stock box.

Also exercised: setup switching with a held pick, Copy XYZ, Copy G-code, reference point, remove
mesh, a wrong stock file (rejected with the reason, the current IPW kept), cancel with Escape,
document close, add-in unload and reload.

## Architecture

```
FusionIPWInspector/
  FusionIPWInspector.py         run()/stop(): toolbar button, commands, save hook
  commands/
    inspector_command.py        the dialog
    actions_command.py          document changes as one undo step, dialog resume
    self_test_command.py        hidden self test
  core/                         pure Python, no Fusion imports
    transform.py                SetupFrame: WCS matrix -> world/setup transform
    point_inspector.py          readings, rounding, copy formats
    stock_file.py               STL reading, unit and frame inference
  stock/
    provider.py                 StockResult, source constants
    post_export.py              PostStockProvider (primary)
    saved_file.py               SavedStockProvider + FolderWatcher (fallback)
    temporary_mesh.py           the tagged temporary component
    mesh_validation.py          sanity checks (pure Python)
    acquisition.py              per-document session, provider chain, save hook
  ui/                           toolbar.py, marker.py (custom graphics)
  diagnostics/                  log.py (quiet by default), self_test.py
  utils/                        prefs.py, clipboard.py, fusion_units.py
  resources/                    icons, post/ipw_inspector_stock.cps
  tests/                        unit tests
tools/validate_in_fusion.py     analytic validation script (run inside Fusion)
```

The transform never depends on how the stock was obtained. Providers only produce a
`StockResult` and a placed mesh, so a future direct API accessor replaces `stock/post_export.py`
and nothing else.

Two Fusion behaviours shape the command design: graphics and document changes made inside a
dialog's `inputChanged` are discarded, so graphics are drawn in `executePreview` and document
changes run in a dialog-less command after which the dialog reopens with its state; and
`ConstructionPointInput.setByPoint` accepts a bare point only in direct-modelling designs, so
reference points are anchored to a sketch point (one sketch collects them). No workspace switch,
no camera change.

## Compatibility and stability

| Part | Status |
|------|--------|
| Setup WCS transform, point readings, copy, reference point | Stable, documented public API |
| Stock export through the post engine (`this.exportStock`, `autodeskcam:stock-path`, `NCPrograms`) | Documented post-processor and API behaviour; the helper post is versioned and reinstalled when it changes |
| Temporary mesh import (`MeshBodies.add` in a base feature) | Stable, documented public API |
| Toolbar placement in panel `CAMInspectPanel` | Falls back to a plain button if the panel moves |
| Folder watcher | Win32 change notifications; macOS falls back to manual loading |

No undocumented command ids and no UI automation are used. Tested on Fusion 2.0.2705x
(2705.1.15) on Windows 11 with the bundled Python 3.14, on a 3440 x 1440 display. The dialog is a
native Fusion command dialog, so it docks where Fusion puts its own dialogs; no separate windows
are created.

## Known limitations

- The stock comes from Fusion's in-process stock computation at simulation resolution (the Setup's
  stock accuracy). Readings on it are labelled accordingly; model readings are exact.
- A setup without a generated operation has no in-process stock to export. Add or generate one.
- Loading the stock and creating a reference point briefly close and reopen the dialog (Fusion
  requires document changes to happen outside the dialog). Setup, unit, pick and Advanced state
  are kept.
- The picked location is the exact viewport hit under the cursor, not a snapped feature. Turn on
  *Also pick model geometry* and use vertices, sketch or construction points when you need a
  snapped reference.
- Turning setups with *From preceding setup* have not been tested.

## Troubleshooting

- **No button.** Check the folder name and that the add-in is running (Utilities > Add-Ins). The
  button exists only in the Manufacture workspace.
- **"No IPW available: … has no generated toolpath yet."** Generate at least one operation of the
  setup, then Advanced > Refresh IPW.
- **"Unmachined stock box."** The setup's incoming stock is the raw stock: the preceding setup has
  no generated operations, or the setup lacks the *From preceding setup* flag (see above).
- **"The stock export post was installed but …"** Open Manage > Post Library once so Fusion
  rescans the personal folder, then refresh.
- **Diagnostics.** Advanced > Diagnostics log writes `%LOCALAPPDATA%\FusionIPWInspector\diagnostics.log`;
  errors are always logged. The cache lives in `%LOCALAPPDATA%\FusionIPWInspector\cache`.

## Privacy and safety

Runs locally, makes no network requests, collects nothing. It never changes a setup, a WCS or an
operation. It creates: one helper post in your personal post folder (a text file, safe to delete),
a temporary NC program that is deleted within the same command, the temporary stock component
(tagged, removed before save), reference points you ask for, and a preferences file plus a cache
folder under your local app data.

## Contributing

Keep `core/` and `stock/mesh_validation.py` free of Fusion imports so the tests stay runnable
anywhere, keep the dialog small, and add a row to *Verified results* when you touch the transform.

## License

MIT, see `LICENSE`.
