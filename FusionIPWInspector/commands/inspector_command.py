"""The "IPW Inspector" command: pick a point, read X/Y/Z in a Setup WCS.

Layout of the dialog (top to bottom):

    Setup            [Setup5 v]
    origin (0, 0, -0.78) mm, X = world +Z, ...
    Point            <click a point on the stock or model>
    X  +12.384 mm
    Y  -21.750 mm
    Z   +6.125 mm
    [Copy XYZ]  [Copy as X.. Y.. Z..]  [Create reference point]
    <stock status>
    [Load saved stock...]
    > Advanced

Reading a point never changes the document. The three actions that do
(loading/removing the temporary stock, creating a reference point) are handed
to ``actions_command`` so they are committed as normal undo steps; the dialog
then reopens where it was.
"""
from __future__ import annotations

import os
import traceback
from typing import List, Optional

import adsk.cam
import adsk.core
import adsk.fusion

from ..core.ipw_provider import StockFileProvider, TemporaryStock
from ..core.point_inspector import InspectedPoint, normalize_unit
from ..core.setup_transform import SetupFrame, SetupFrameError
from ..ui import toolbar
from ..ui.markers import Markers
from ..utils import clipboard, log
from ..utils.fusion_units import api_point_to_mm, document_length_unit, wcs_matrix_rows_mm
from ..utils.prefs import Preferences
from .actions_command import ActionCommand, PendingAction

COMMAND_ID = 'FusionIPWInspector_Inspect'
COMMAND_NAME = 'IPW Inspector'
COMMAND_TOOLTIP = 'Click a point on the remaining (in-process) stock and read its X, Y, Z in a Setup WCS.'
RESOURCES = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources', 'ipw_inspector')
STOCK_FILTER = 'Saved stock (*.stl;*.3mf;*.obj);;All files (*.*)'

# Input ids
IN_SETUP = 'setup'
IN_FRAME = 'frame_info'
IN_PICK = 'pick'
IN_RESULT = 'result'
IN_COPY_XYZ = 'copy_xyz'
IN_COPY_MACHINE = 'copy_machine'
IN_CREATE_POINT = 'create_point'
IN_STOCK_STATUS = 'stock_status'
IN_LOAD_STOCK = 'load_stock'
IN_ADVANCED = 'advanced'
IN_UNITS = 'units'
IN_SHOW_TRIAD = 'show_triad'
IN_REMOVE_STOCK = 'remove_stock'
IN_DIAGNOSTICS = 'diagnostics'

SELECTION_FILTERS = ('MeshBodies', 'SolidBodies', 'SurfaceBodies', 'Faces', 'Edges', 'Vertices',
                     'SketchPoints', 'ConstructionPoints')

_handlers: List[adsk.core.EventHandler] = []   # keeps handler objects alive


class InspectorCommand:
    """Owns the command definition, the helper action command and the per-dialog session."""

    def __init__(self, app: adsk.core.Application, prefs: Preferences) -> None:
        self.app = app
        self.ui = app.userInterface
        self.prefs = prefs
        self.session: Optional['_Session'] = None
        self.actions = ActionCommand(app, self._reopen)
        self._definition: Optional[adsk.core.CommandDefinition] = None
        self._providers = {}          # document name -> StockFileProvider (cache of loaded stock)
        self.resume: Optional[dict] = None   # state to restore when the dialog reopens after an action

    # ------------------------------------------------------------ lifecycle
    def register(self) -> None:
        definitions = self.ui.commandDefinitions
        existing = definitions.itemById(COMMAND_ID)
        if existing:
            existing.deleteMe()
        self._definition = definitions.addButtonDefinition(COMMAND_ID, COMMAND_NAME, COMMAND_TOOLTIP, RESOURCES)
        self._definition.toolClipFilename = os.path.join(RESOURCES, '64x64.png')
        on_created = _CommandCreatedHandler(self)
        self._definition.commandCreated.add(on_created)
        _handlers.append(on_created)
        self.actions.register()
        toolbar.add_button(self.ui, self._definition)

    def unregister(self) -> None:
        if self.session is not None:
            self.session.close()
        toolbar.remove_button(self.ui, COMMAND_ID)
        for provider in self._providers.values():
            try:
                provider.unload()
            except Exception as exc:
                log.error('cleanup of temporary stock failed', exc)
        self._providers.clear()
        self.actions.unregister()
        if self._definition:
            try:
                self._definition.deleteMe()
            except Exception:
                pass
            self._definition = None
        _handlers.clear()

    def provider_for(self, design: adsk.fusion.Design) -> StockFileProvider:
        key = design.parentDocument.name if design and design.parentDocument else 'default'
        provider = self._providers.get(key)
        if provider is None or not _is_valid(provider.design):
            provider = StockFileProvider(design)
            self._providers[key] = provider
        else:
            # API proxies are recreated on every call; keep the provider on a live one.
            provider.design = design
            provider.temporary.design = design
        return provider

    def _reopen(self) -> None:
        if self._definition:
            self._definition.execute()


# ------------------------------------------------------------------ session
class _Session:
    """State of one open dialog."""

    def __init__(self, owner: InspectorCommand, command: adsk.core.Command) -> None:
        self.owner = owner
        self.app = owner.app
        self.ui = owner.ui
        self.prefs = owner.prefs
        self.command = command
        self.inputs = command.commandInputs
        self.cam: Optional[adsk.cam.CAM] = None
        self.design: Optional[adsk.fusion.Design] = None
        self.setups: List[adsk.cam.Setup] = []
        self.frame: Optional[SetupFrame] = None
        self.point: Optional[InspectedPoint] = None
        self.markers: Optional[Markers] = None
        self.provider: Optional[StockFileProvider] = None
        self._busy = False
        self.unit = 'mm'

    # ------------------------------------------------------------- building
    def build(self) -> bool:
        doc = self.app.activeDocument
        if doc is None:
            self.ui.messageBox('Open a design with a Manufacture setup first.', COMMAND_NAME)
            return False
        cam_product = doc.products.itemByProductType('CAMProductType')
        design_product = doc.products.itemByProductType('DesignProductType')
        self.cam = adsk.cam.CAM.cast(cam_product) if cam_product else None
        self.design = adsk.fusion.Design.cast(design_product) if design_product else None
        if self.cam is None or self.design is None:
            self.ui.messageBox('This document has no Manufacture data yet. Switch to the Manufacture '
                               'workspace, create a Setup, then run IPW Inspector.', COMMAND_NAME)
            return False
        self.setups = [s for s in self.cam.setups]
        if not self.setups:
            self.ui.messageBox('There is no Setup in this document. Create a Setup in Manufacture, '
                               'then run IPW Inspector.', COMMAND_NAME)
            return False
        self.unit = normalize_unit(document_length_unit(self.design))
        self.provider = self.owner.provider_for(self.design)
        self.markers = Markers(self._graphics_groups(), _model_size_mm(self.design))
        log.set_enabled(bool(self.prefs.get('diagnostics')))
        resume = self.owner.resume
        self.owner.resume = None

        cmd = self.command
        cmd.isOKButtonVisible = False
        cmd.cancelButtonText = 'Close'
        cmd.isRepeatable = False
        cmd.setDialogInitialSize(330, 560)
        cmd.setDialogMinimumSize(300, 460)

        inputs = self.inputs
        setup_dd = inputs.addDropDownCommandInput(IN_SETUP, 'Setup', adsk.core.DropDownStyles.TextListDropDownStyle)
        active = self._initial_setup(resume.get('setup_name') if resume else None)
        for s in self.setups:
            setup_dd.listItems.add(s.name, s is active)
        setup_dd.tooltip = 'Coordinates are reported relative to this setup\'s work coordinate system (WCS).'

        frame_box = inputs.addTextBoxCommandInput(IN_FRAME, 'WCS', '', 2, True)
        frame_box.isFullWidth = True

        pick = inputs.addSelectionInput(IN_PICK, 'Point', 'Click a point on the remaining stock or the model')
        for f in SELECTION_FILTERS:
            pick.addSelectionFilter(f)
        pick.setSelectionLimits(0, 1)
        pick.tooltip = 'Click anywhere on the stock, model, a face, an edge or a point. The clicked location is used.'

        result = inputs.addTextBoxCommandInput(IN_RESULT, ' ', '', 5, True)
        result.isFullWidth = True

        _button(inputs, IN_COPY_XYZ, 'Copy XYZ',
                'Copy the three values, tab separated, to the clipboard.')
        _button(inputs, IN_COPY_MACHINE, 'Copy as X.. Y.. Z..',
                'Copy in controller style, e.g. X12.384 Y-21.750 Z6.125')
        _button(inputs, IN_CREATE_POINT, 'Create reference point',
                'Add a construction point at the picked location (in the design, named with its WCS coordinates).')

        stock_status = inputs.addTextBoxCommandInput(IN_STOCK_STATUS, 'Stock', '', 3, True)
        stock_status.isFullWidth = True
        _button(inputs, IN_LOAD_STOCK, 'Load saved stock...',
                'Load the stock you saved from Simulation (right-click the stock > Stock > Save Stock...). '
                'It is placed as a temporary, clearly named mesh so you can click on it.')

        adv = inputs.addGroupCommandInput(IN_ADVANCED, 'Advanced')
        adv.isExpanded = bool(self.prefs.get('advanced_expanded'))
        adv_inputs = adv.children
        units = adv_inputs.addDropDownCommandInput(IN_UNITS, 'Units', adsk.core.DropDownStyles.TextListDropDownStyle)
        chosen = resume.get('units') if resume else None
        units.listItems.add('Document (%s)' % self.unit, chosen not in ('mm', 'in'))
        units.listItems.add('mm', chosen == 'mm')
        units.listItems.add('in', chosen == 'in')
        adv_inputs.addBoolValueInput(IN_SHOW_TRIAD, 'Show WCS triad', True, '', bool(self.prefs.get('show_triad')))
        _button(adv_inputs, IN_REMOVE_STOCK, 'Remove temporary stock',
                'Delete the temporary stock component this tool created.')
        adv_inputs.addBoolValueInput(IN_DIAGNOSTICS, 'Diagnostics log', True, '', bool(self.prefs.get('diagnostics'))).tooltip = \
            'Write a diagnostics log to ' + log.log_path()

        self._apply_setup(active)
        if resume and resume.get('point') is not None and self.frame is not None:
            p = resume['point']
            self.point = InspectedPoint.from_world(p.world_xyz_mm, self.frame, p.source_kind, p.source_name)
        self._update_result()
        self._update_stock_status(self.owner.actions.last_status)
        self.owner.actions.last_status = ''
        pick.hasFocus = True
        return True

    def _graphics_groups(self):
        try:
            if self.app.activeProduct and self.app.activeProduct.productType == 'CAMProductType':
                return self.cam.customGraphicsGroups
        except Exception:
            pass
        return self.design.rootComponent.customGraphicsGroups

    def _initial_setup(self, preferred_name: Optional[str]) -> adsk.cam.Setup:
        if preferred_name:
            for s in self.setups:
                if s.name == preferred_name:
                    return s
        for s in self.setups:
            if s.isActive:
                return s
        try:
            for i in range(self.ui.activeSelections.count):
                ent = adsk.cam.Setup.cast(self.ui.activeSelections.item(i).entity)
                if ent:
                    return ent
        except Exception:
            pass
        return self.setups[0]

    # ------------------------------------------------------------- updates
    def _selected_setup(self) -> adsk.cam.Setup:
        dd = adsk.core.DropDownCommandInput.cast(self.inputs.itemById(IN_SETUP))
        item = dd.selectedItem
        for s in self.setups:
            if item and s.name == item.name:
                return s
        return self.setups[0]

    def _apply_setup(self, setup: adsk.cam.Setup) -> None:
        frame_box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_FRAME))
        try:
            self.frame = SetupFrame.from_matrix_rows(wcs_matrix_rows_mm(setup.workCoordinateSystem), setup.name)
            frame_box.text = self.frame.describe()
            log.info('setup "%s": %s' % (setup.name, self.frame.describe()))
        except SetupFrameError as exc:
            self.frame = None
            frame_box.text = 'The WCS of %s could not be read (%s). Open the setup, check its WCS, then try again.' % (setup.name, exc)
            log.error('bad WCS for setup %s' % setup.name, exc)
        if self.point is not None and self.frame is not None:
            self.point = InspectedPoint.from_world(self.point.world_xyz_mm, self.frame,
                                                   self.point.source_kind, self.point.source_name)
        self._draw_triad()

    def _display_unit(self) -> str:
        dd = adsk.core.DropDownCommandInput.cast(self.inputs.itemById(IN_UNITS))
        if dd and dd.selectedItem and dd.selectedItem.name in ('mm', 'in'):
            return dd.selectedItem.name
        return self.unit

    def _update_result(self) -> None:
        box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_RESULT))
        have_point = self.point is not None and self.frame is not None
        for bid in (IN_COPY_XYZ, IN_COPY_MACHINE, IN_CREATE_POINT):
            self.inputs.itemById(bid).isEnabled = have_point
        if not have_point:
            box.formattedText = ('<div style="color:#8a8a8a">Pick a point on the remaining stock.<br>'
                                 'Coordinates will show here relative to the selected setup WCS.</div>')
            return
        unit = self._display_unit()
        rows = ''.join('<tr><td style="padding-right:12px"><b>%s</b></td><td align="right"><b>%s</b></td>'
                       '<td>&nbsp;%s</td></tr>' % (line[0], line[3:].rsplit(' ', 1)[0].strip(), unit)
                       for line in self.point.formatted_lines(unit))
        note = '%s WCS &middot; %s' % (self.point.setup_name, self.point.source_label())
        if self.point.is_approximate():
            note += '<br><span style="color:#8a8a8a">Simulation stock: accuracy depends on simulation resolution.</span>'
        box.formattedText = ('<table style="font-family:Consolas,monospace;font-size:14px">%s</table>'
                             '<div style="font-size:11px">%s</div>' % (rows, note))

    def _update_stock_status(self, extra: str = '') -> None:
        box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_STOCK_STATUS))
        loaded = self.provider.current if self.provider else None
        if loaded is not None:
            try:
                alive = loaded.occurrence.isValid
            except Exception:
                alive = False
            if alive:
                text = 'Stock: %s (read as %s)' % (os.path.basename(loaded.file_path), loaded.interpretation.label)
                if loaded.notes:
                    text += '\n' + ' '.join(loaded.notes)
                box.text = (extra + '\n' + text) if extra else text
                return
        leftovers = TemporaryStock(self.design).find_all() if self.design else []
        if leftovers:
            text = 'Temporary stock is present (loaded earlier). Click it, or load a newer file.'
        else:
            text = ('No saved stock loaded. You can click the model directly, or load the stock '
                    'saved from Simulation.')
        box.text = (extra + '\n' + text) if extra else text

    def _draw_triad(self) -> None:
        show = adsk.core.BoolValueCommandInput.cast(self.inputs.itemById(IN_SHOW_TRIAD))
        if self.frame is not None and show and show.value:
            self.markers.show_triad(self.frame)
        else:
            self.markers.clear_triad()

    def redraw(self) -> None:
        """Draw all viewport graphics. Called from the command's executePreview event.

        Fusion discards custom graphics created inside inputChanged, so every
        change of the pick or the setup only records state and asks for a
        preview; the actual drawing happens here.
        """
        self._draw_triad()
        self._draw_point()

    def _request_redraw(self) -> None:
        try:
            self.command.doExecutePreview()
        except Exception as exc:
            log.error('doExecutePreview failed', exc)

    # ------------------------------------------------------------- actions
    def on_input_changed(self, changed: adsk.core.CommandInput) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            cid = changed.id
            if cid == IN_SETUP:
                self._apply_setup(self._selected_setup())
                self._update_result()
                self._request_redraw()
            elif cid == IN_PICK:
                self._on_pick(adsk.core.SelectionCommandInput.cast(changed))
            elif cid == IN_UNITS:
                self._update_result()
                self._request_redraw()
            elif cid == IN_SHOW_TRIAD:
                self.prefs.set('show_triad', bool(adsk.core.BoolValueCommandInput.cast(changed).value))
                self._request_redraw()
            elif cid == IN_DIAGNOSTICS:
                value = bool(adsk.core.BoolValueCommandInput.cast(changed).value)
                self.prefs.set('diagnostics', value)
                log.set_enabled(value)
            elif cid == IN_ADVANCED:
                self.prefs.set('advanced_expanded', bool(adsk.core.GroupCommandInput.cast(changed).isExpanded))
            elif cid in (IN_COPY_XYZ, IN_COPY_MACHINE, IN_CREATE_POINT, IN_LOAD_STOCK, IN_REMOVE_STOCK):
                button = adsk.core.BoolValueCommandInput.cast(changed)
                if button.value:
                    button.value = False
                    self._run_button(cid)
        except Exception as exc:
            log.error('input change failed', exc)
            self.ui.messageBox('IPW Inspector hit an unexpected error:\n%s' % exc, COMMAND_NAME)
        finally:
            self._busy = False

    def _run_button(self, cid: str) -> None:
        if cid == IN_COPY_XYZ:
            self._copy(self.point.clipboard_text(self._display_unit()), 'xyz')
        elif cid == IN_COPY_MACHINE:
            self._copy(self.point.machine_text(self._display_unit()), 'machine')
        elif cid == IN_CREATE_POINT:
            self._create_reference_point()
        elif cid == IN_LOAD_STOCK:
            self._load_stock()
        elif cid == IN_REMOVE_STOCK:
            self._run_action(PendingAction('remove_stock', provider=self.provider))

    def _on_pick(self, pick: adsk.core.SelectionCommandInput) -> None:
        if pick.selectionCount == 0:
            return
        selection = pick.selection(0)
        entity = selection.entity
        try:
            world_mm = api_point_to_mm(selection.point)
        except Exception:
            pick.clearSelection()
            return
        kind, name = _classify(entity)
        if self.frame is None:
            self._update_result()
            pick.clearSelection()
            return
        self.point = InspectedPoint.from_world(world_mm, self.frame, kind, name)
        log.info('picked %s "%s" world %s mm -> %s %s mm' % (kind, name, _r(world_mm), self.point.setup_name,
                                                             _r(self.point.setup_xyz_mm)))
        self._update_result()
        # Clear the selection so the very next click is a new pick; keep the marker.
        pick.clearSelection()
        pick.hasFocus = True
        self._request_redraw()

    def _draw_point(self) -> None:
        if self.point is None:
            self.markers.clear_point()
            return
        unit = self._display_unit()
        label = ' '.join(l.replace('  ', ' ') for l in self.point.formatted_lines(unit))
        self.markers.show_point(self.point.world_xyz_mm, label)

    def _copy(self, text: str, fmt: str) -> None:
        ok = clipboard.copy_text(text)
        self.prefs.set('copy_format', fmt)
        shown = text.replace('\t', '   ')
        status = 'Copied to clipboard: %s' % shown if ok else 'Could not access the clipboard. Value: %s' % shown
        self._update_stock_status(status)

    def _create_reference_point(self) -> None:
        if self.point is None:
            return
        name = 'IPW %s %s' % (self.point.setup_name, self.point.machine_text(self._display_unit()))
        self._run_action(PendingAction('reference_point', design=self.design,
                                       world_xyz_mm=self.point.world_xyz_mm, name=name))

    def _load_stock(self) -> None:
        dialog = self.ui.createFileDialog()
        dialog.title = 'Load stock saved from Simulation'
        dialog.filter = STOCK_FILTER
        dialog.isMultiSelectEnabled = False
        last = self.prefs.get('last_stock_folder') or ''
        if last and os.path.isdir(last):
            dialog.initialDirectory = last
        if dialog.showOpen() != adsk.core.DialogResults.DialogOK:
            return
        path = dialog.filename
        self.prefs.set('last_stock_folder', os.path.dirname(path))
        frames = []
        for s in self.setups:
            try:
                frames.append(SetupFrame.from_matrix_rows(wcs_matrix_rows_mm(s.workCoordinateSystem), s.name))
            except SetupFrameError:
                pass
        self._run_action(PendingAction('load_stock', provider=self.provider, path=path,
                                       setup=self._selected_setup(), frames=frames))

    def _run_action(self, action: PendingAction) -> None:
        """Hand a document change to the action command; the dialog closes and reopens afterwards."""
        self.owner.resume = {
            'setup_name': self._selected_setup().name,
            'point': self.point,
            'units': self._display_unit() if self._display_unit() != self.unit else None,
        }
        self.owner.actions.run(action)

    # -------------------------------------------------------------- closing
    def close(self) -> None:
        try:
            if self.markers:
                self.markers.clear()
        except Exception:
            pass
        self.owner.session = None


# ----------------------------------------------------------------- handlers
class _CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CommandCreatedEventArgs) -> None:
        try:
            command = args.command
            session = _Session(self.owner, command)
            if not session.build():
                return
            self.owner.session = session
            for event, handler in ((command.inputChanged, _InputChangedHandler(session)),
                                   (command.executePreview, _ExecutePreviewHandler(session)),
                                   (command.destroy, _DestroyHandler(session)),
                                   (command.execute, _ExecuteHandler(session))):
                event.add(handler)
                _handlers.append(handler)
        except Exception as exc:
            log.error('command creation failed', exc)
            adsk.core.Application.get().userInterface.messageBox(
                'IPW Inspector could not open:\n%s' % traceback.format_exc(), COMMAND_NAME)


class _InputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.InputChangedEventArgs) -> None:
        self.session.on_input_changed(args.input)


class _ExecutePreviewHandler(adsk.core.CommandEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        try:
            self.session.redraw()
        except Exception as exc:
            log.error('redraw failed', exc)


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        pass  # the dialog only has a Close button; nothing to commit


class _DestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        self.session.close()


# ------------------------------------------------------------------ helpers
def _button(inputs: adsk.core.CommandInputs, input_id: str, text: str, tooltip: str) -> adsk.core.BoolValueCommandInput:
    """A full-width push button (a BoolValueCommandInput shown as a button)."""
    button = inputs.addBoolValueInput(input_id, text, False, '', False)
    button.text = text
    button.isFullWidth = True
    button.tooltip = tooltip
    return button


def _classify(entity) -> tuple:
    try:
        if adsk.fusion.MeshBody.cast(entity):
            return 'mesh', entity.name
        if adsk.fusion.BRepBody.cast(entity) or adsk.fusion.BRepFace.cast(entity) or adsk.fusion.BRepEdge.cast(entity):
            body = getattr(entity, 'body', entity)
            return 'brep', getattr(body, 'name', '')
        if adsk.fusion.BRepVertex.cast(entity) or adsk.fusion.SketchPoint.cast(entity) or \
                adsk.fusion.ConstructionPoint.cast(entity):
            return 'point', getattr(entity, 'name', '')
    except Exception:
        pass
    return 'brep', ''


def _r(v) -> str:
    return '(%.4f, %.4f, %.4f)' % tuple(v)


def _model_size_mm(design: adsk.fusion.Design) -> float:
    """Diagonal of everything visible in the design (mm), used to scale viewport graphics."""
    try:
        box = None
        for occ_or_body in list(design.rootComponent.bRepBodies) + [o for o in design.rootComponent.allOccurrences]:
            bb = occ_or_body.boundingBox
            if box is None:
                box = [bb.minPoint.x, bb.minPoint.y, bb.minPoint.z, bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z]
            else:
                box = [min(box[0], bb.minPoint.x), min(box[1], bb.minPoint.y), min(box[2], bb.minPoint.z),
                       max(box[3], bb.maxPoint.x), max(box[4], bb.maxPoint.y), max(box[5], bb.maxPoint.z)]
        if box is None:
            return 50.0
        diag = ((box[3] - box[0]) ** 2 + (box[4] - box[1]) ** 2 + (box[5] - box[2]) ** 2) ** 0.5
        return max(1.0, diag * 10.0)
    except Exception:
        return 50.0


def _is_valid(obj) -> bool:
    try:
        return bool(obj is not None and obj.isValid)
    except Exception:
        return False
