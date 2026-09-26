"""The "IPW Inspector" dialog: pick a point, read X/Y/Z in a Setup WCS.

    Setup      [Setup5 v]
    Current IPW - Setup5
    Point      [Select]
    X   +10.778 mm
    Y    -0.684 mm
    Z    -0.040 mm
    on IPW
    [Copy XYZ]
    [Copy G-code]
    > Advanced

Reading a point never changes the document. Everything that does (getting
the stock, loading a file, removing the mesh, creating a reference point) goes
through ``actions_command`` and the dialog reopens where it was.
"""
from __future__ import annotations

import os
import traceback
from typing import List, Optional

import adsk.cam
import adsk.core
import adsk.fusion

from ..core.point_inspector import InspectedPoint, normalize_unit
from ..core.transform import SetupFrame
from ..diagnostics import log
from ..stock.acquisition import StockSession, StockSessions
from ..stock.provider import SOURCE_CURRENT, SOURCE_NONE, SOURCE_SAVED, StockResult
from ..stock.saved_file import FolderWatcher
from ..ui.marker import Markers
from ..utils import clipboard
from ..utils.fusion_units import api_point_to_mm, document_length_unit
from ..utils.prefs import Preferences
from .actions_command import ActionCommand, PendingAction

COMMAND_ID = 'FusionIPWInspector_Inspect'
COMMAND_NAME = 'IPW Inspector'
COMMAND_TOOLTIP = 'Click a point on the in-process stock (or the model) and read its X, Y, Z in a Setup WCS.'
RESOURCES = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources', 'ipw_inspector')
STOCK_FILTER = 'Saved stock (*.stl);;All files (*.*)'
NEW_FILE_EVENT_ID = 'FusionIPWInspector_NewStockFile'

IN_SETUP = 'setup'
IN_SOURCE = 'source'
IN_PICK = 'pick'
IN_RESULT = 'result'
IN_COPY_XYZ = 'copy_xyz'
IN_COPY_GCODE = 'copy_gcode'
IN_ADVANCED = 'advanced'
IN_UNITS = 'units'
IN_SHOW_TRIAD = 'show_triad'
IN_PICK_MODEL = 'pick_model'
IN_CREATE_POINT = 'create_point'
IN_REFRESH = 'refresh'
IN_LOAD_STOCK = 'load_stock'
IN_REMOVE_STOCK = 'remove_stock'
IN_WCS_INFO = 'wcs_info'
IN_DIAG_LOG = 'diag_log'
IN_SELF_TEST = 'self_test'

SELECTION_FILTERS = ('MeshBodies', 'SolidBodies', 'SurfaceBodies', 'Faces', 'Edges', 'Vertices',
                     'SketchPoints', 'ConstructionPoints')
GREY = '#8a8a8a'

_handlers: List[adsk.core.EventHandler] = []


class InspectorCommand:
    """Owns the command definitions and the per-dialog session."""

    def __init__(self, app: adsk.core.Application, prefs: Preferences, sessions: StockSessions,
                 self_test: Optional[object] = None) -> None:
        self.app = app
        self.ui = app.userInterface
        self.prefs = prefs
        self.sessions = sessions
        self.self_test = self_test
        self.session: Optional['_Session'] = None
        self.resume: Optional[dict] = None
        self.actions = ActionCommand(app, sessions, self._reopen, self._pick_setup)
        self._definition: Optional[adsk.core.CommandDefinition] = None
        self._new_file_event: Optional[adsk.core.CustomEvent] = None
        self.watcher: Optional[FolderWatcher] = None

    # ------------------------------------------------------------ lifecycle
    def register(self) -> adsk.core.CommandDefinition:
        """Register the dialog and the action commands; returns the toolbar (open) definition."""
        definitions = self.ui.commandDefinitions
        existing = definitions.itemById(COMMAND_ID)
        if existing:
            existing.deleteMe()
        self._definition = definitions.addButtonDefinition(COMMAND_ID, COMMAND_NAME, COMMAND_TOOLTIP, RESOURCES)
        try:
            # Only the toolbar (open) command should show up in searches; the dialog is opened by it.
            self._definition.controlDefinition.isVisible = False
        except Exception:
            pass
        created = _CommandCreatedHandler(self)
        self._definition.commandCreated.add(created)
        _handlers.append(created)
        try:
            self.app.unregisterCustomEvent(NEW_FILE_EVENT_ID)
        except Exception:
            pass
        self._new_file_event = self.app.registerCustomEvent(NEW_FILE_EVENT_ID)
        new_file = _NewFileHandler(self)
        self._new_file_event.add(new_file)
        _handlers.append(new_file)
        open_definition = self.actions.register(COMMAND_NAME, COMMAND_TOOLTIP, RESOURCES)
        open_definition.toolClipFilename = os.path.join(RESOURCES, '64x64.png')
        return open_definition

    def unregister(self) -> None:
        self.stop_watcher()
        if self.session is not None:
            self.session.close()
        try:
            self.app.unregisterCustomEvent(NEW_FILE_EVENT_ID)
        except Exception:
            pass
        self.actions.unregister()
        if self._definition:
            try:
                self._definition.deleteMe()
            except Exception:
                pass
            self._definition = None
        _handlers.clear()

    def _reopen(self) -> None:
        if self._definition:
            self._definition.execute()

    def _pick_setup(self) -> Optional[adsk.cam.Setup]:
        """The setup the user means: the resumed one, else the active one, else the selected one."""
        doc = self.app.activeDocument
        cam, _ = StockSession.products(doc) if doc else (None, None)
        if cam is None or cam.setups.count == 0:
            return None
        wanted = (self.resume or {}).get('setup_name')
        setups = [s for s in cam.setups]
        for s in setups:
            if wanted and s.name == wanted:
                return s
        for s in setups:
            if s.isActive:
                return s
        try:
            for i in range(self.ui.activeSelections.count):
                ent = self.ui.activeSelections.item(i).entity
                setup = adsk.cam.Setup.cast(ent)
                if setup:
                    return setup
                op = adsk.cam.Operation.cast(ent)
                if op and op.parentSetup:
                    return op.parentSetup
        except Exception:
            pass
        return setups[0]

    # -------------------------------------------------------------- watcher
    def start_watcher(self, folder: str) -> None:
        self.stop_watcher()
        if folder and os.path.isdir(folder):
            self.watcher = FolderWatcher(folder, self._on_new_file_background)
            self.watcher.start()

    def stop_watcher(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None

    def _on_new_file_background(self, path: str) -> None:
        try:
            self.app.fireCustomEvent(NEW_FILE_EVENT_ID, path)
        except Exception:
            pass

    def on_new_file(self, path: str) -> None:
        """Main thread: a stock file appeared in the watched folder while the dialog is open."""
        session = self.session
        if session is None or session.result is not None and session.result.source == SOURCE_CURRENT:
            return
        log.info('new stock file detected: %s' % path)
        session.run_action(PendingAction('load_saved', setup=session.selected_setup(), path=path))


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
        self.doc: Optional[adsk.core.Document] = None
        self.cam: Optional[adsk.cam.CAM] = None
        self.design: Optional[adsk.fusion.Design] = None
        self.stock: Optional[StockSession] = None
        self.setups: List[adsk.cam.Setup] = []
        self.frame: Optional[SetupFrame] = None
        self.point: Optional[InspectedPoint] = None
        self.result: Optional[StockResult] = None
        self.markers: Optional[Markers] = None
        self.unit = 'mm'
        self._busy = False
        self._status = ''

    # ------------------------------------------------------------- building
    def build(self) -> bool:
        self.doc = self.app.activeDocument
        if self.doc is None:
            self.ui.messageBox('Open a design with a Manufacture setup first.', COMMAND_NAME)
            return False
        self.cam, self.design = StockSession.products(self.doc)
        if self.cam is None or self.design is None:
            self.ui.messageBox('This document has no Manufacture data yet. Switch to the Manufacture '
                               'workspace, create a Setup, then run IPW Inspector.', COMMAND_NAME)
            return False
        self.setups = [s for s in self.cam.setups]
        if not self.setups:
            self.ui.messageBox('There is no Setup in this document. Create a Setup in Manufacture, '
                               'then run IPW Inspector.', COMMAND_NAME)
            return False
        self.stock = self.owner.sessions.for_document(self.doc)
        self.unit = normalize_unit(document_length_unit(self.design))
        self.markers = Markers(self._graphics_groups(), _model_size_mm(self.design))
        log.set_enabled(bool(self.prefs.get('diagnostics')))
        resume = self.owner.resume or {}
        self.owner.resume = None
        self._status = self.owner.actions.last_status
        self.owner.actions.last_status = ''

        cmd = self.command
        cmd.isOKButtonVisible = False
        cmd.cancelButtonText = 'Close'
        cmd.isRepeatable = False
        cmd.setDialogInitialSize(320, 470)
        cmd.setDialogMinimumSize(300, 400)

        inputs = self.inputs
        setup = self._initial_setup(resume.get('setup_name'))
        dd = inputs.addDropDownCommandInput(IN_SETUP, 'Setup', adsk.core.DropDownStyles.TextListDropDownStyle)
        for s in self.setups:
            dd.listItems.add(s.name, s is setup)
        dd.tooltip = 'Coordinates are relative to this setup\'s work coordinate system (WCS).'

        source = inputs.addTextBoxCommandInput(IN_SOURCE, ' ', '', 3, True)
        source.isFullWidth = True

        pick = inputs.addSelectionInput(IN_PICK, 'Point', 'Click a point on the in-process stock or the model')
        pick.setSelectionLimits(0, 1)
        pick.tooltip = 'Click on the stock (or the model when no IPW is loaded). The clicked location is used.'

        result = inputs.addTextBoxCommandInput(IN_RESULT, ' ', '', 4, True)
        result.isFullWidth = True

        _button(inputs, IN_COPY_XYZ, 'Copy XYZ', 'Copy the three values, tab separated.')
        _button(inputs, IN_COPY_GCODE, 'Copy G-code', 'Copy as X.. Y.. Z.., for example X12.384 Y-21.750 Z6.125')

        adv = inputs.addGroupCommandInput(IN_ADVANCED, 'Advanced')
        adv.isExpanded = bool(resume.get('advanced', self.prefs.get('advanced_expanded')))
        a = adv.children
        units = a.addDropDownCommandInput(IN_UNITS, 'Units', adsk.core.DropDownStyles.TextListDropDownStyle)
        chosen = resume.get('units')
        units.listItems.add('Document (%s)' % self.unit, chosen not in ('mm', 'in'))
        units.listItems.add('mm', chosen == 'mm')
        units.listItems.add('in', chosen == 'in')
        a.addBoolValueInput(IN_SHOW_TRIAD, 'Show WCS triad', True, '', bool(self.prefs.get('show_triad')))
        a.addBoolValueInput(IN_PICK_MODEL, 'Also pick model geometry', True, '', bool(resume.get('pick_model', False))).tooltip =             'Fusion prefers solid bodies over meshes when both are under the cursor, so model picking is off while an IPW is loaded.'
        _button(a, IN_CREATE_POINT, 'Create reference point',
                'Add a construction point at the picked location, named with its WCS coordinates.')
        _button(a, IN_REFRESH, 'Refresh IPW', 'Ask Fusion again for the in-process stock of the selected setup.')
        _button(a, IN_LOAD_STOCK, 'Load saved stock...',
                'Use a stock file saved from Simulation (right-click the stock > Stock > Save Stock...).')
        _button(a, IN_REMOVE_STOCK, 'Remove IPW mesh', 'Delete the temporary stock mesh from the document.')
        info = a.addTextBoxCommandInput(IN_WCS_INFO, ' ', '', 3, True)
        info.isFullWidth = True
        a.addBoolValueInput(IN_DIAG_LOG, 'Diagnostics log', True, '', bool(self.prefs.get('diagnostics'))).tooltip = \
            'Log file: ' + log.log_path()
        _button(a, IN_SELF_TEST, 'Run self test', 'Check the transform math, units, setup WCS and the loaded stock.')

        self._apply_setup(setup)
        if resume.get('point') is not None and self.frame is not None:
            p = resume['point']
            self.point = InspectedPoint.from_world(p.world_xyz_mm, self.frame, p.source_kind, p.source_name)
        self._refresh_source()
        self._update_result()
        self._apply_pick_filters()
        pick.hasFocus = True
        return True

    def _apply_pick_filters(self) -> None:
        """With an IPW loaded only the mesh is pickable (unless the user opts in to model picks)."""
        pick = adsk.core.SelectionCommandInput.cast(self.inputs.itemById(IN_PICK))
        allow_model = adsk.core.BoolValueCommandInput.cast(self.inputs.itemById(IN_PICK_MODEL))
        mesh_only = self.result is not None and self.result.mesh_body_valid and not (allow_model and allow_model.value)
        pick.clearSelectionFilter()
        for f in (('MeshBodies',) if mesh_only else SELECTION_FILTERS):
            pick.addSelectionFilter(f)

    def _graphics_groups(self):
        try:
            if self.app.activeProduct and self.app.activeProduct.productType == 'CAMProductType':
                return self.cam.customGraphicsGroups
        except Exception:
            pass
        return self.design.rootComponent.customGraphicsGroups

    def _initial_setup(self, preferred_name: Optional[str]) -> adsk.cam.Setup:
        for s in self.setups:
            if preferred_name and s.name == preferred_name:
                return s
        loaded = self.stock.result
        if loaded is not None:
            for s in self.setups:
                if s.operationId == loaded.setup_id:
                    return s
        picked = self.owner._pick_setup()
        return picked if picked is not None else self.setups[0]

    # --------------------------------------------------------------- state
    def selected_setup(self) -> adsk.cam.Setup:
        dd = adsk.core.DropDownCommandInput.cast(self.inputs.itemById(IN_SETUP))
        item = dd.selectedItem
        for s in self.setups:
            if item and s.name == item.name:
                return s
        return self.setups[0]

    def _apply_setup(self, setup: adsk.cam.Setup) -> None:
        self.frame = StockSession.frame_of(setup)
        info = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_WCS_INFO))
        if self.frame is not None:
            info.text = '%s WCS: %s' % (setup.name, self.frame.describe())
        else:
            info.text = 'The WCS of %s could not be read. Open the setup, check its WCS, then try again.' % setup.name
        if self.point is not None and self.frame is not None:
            self.point = InspectedPoint.from_world(self.point.world_xyz_mm, self.frame,
                                                   self.point.source_kind, self.point.source_name)

    def _display_unit(self) -> str:
        dd = adsk.core.DropDownCommandInput.cast(self.inputs.itemById(IN_UNITS))
        if dd and dd.selectedItem and dd.selectedItem.name in ('mm', 'in'):
            return dd.selectedItem.name
        return self.unit

    def _refresh_source(self) -> None:
        """Update the source line from the stock session and the last action."""
        setup = self.selected_setup()
        self.result = self.stock.current_for(self.doc, setup)
        last = self.owner.actions.last_result
        box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_SOURCE))
        lines = []
        if self.result is not None:
            title = 'Current IPW' if self.result.source == SOURCE_CURRENT else 'Saved IPW'
            lines.append('<b>%s</b> &#10003; &nbsp;%s' % (title, setup.name))
            notes = [n for n in self.result.notes if 'unmachined box' in n]
            if self.result.is_plain_box:
                lines.append('<span style="color:%s">Unmachined stock box. Generate the preceding toolpaths for the machined stock.</span>' % GREY)
            elif notes:
                lines.append('<span style="color:%s">%s</span>' % (GREY, notes[0]))
            self.owner.stop_watcher()
        else:
            reason = ''
            if last is not None and last.setup_id == setup.operationId and last.error:
                reason = last.error
            lines.append('<b>No IPW available</b> &nbsp;%s' % setup.name)
            if reason:
                lines.append('<span style="color:%s">%s</span>' % (GREY, reason))
            else:
                lines.append('<span style="color:%s">Model geometry can still be picked. Use Advanced &gt; Refresh IPW.</span>' % GREY)
            folder = self.prefs.get('last_stock_folder') or ''
            if folder:
                self.owner.start_watcher(folder)
        if self._status:
            lines.append('<span style="color:%s">%s</span>' % (GREY, self._status))
        box.formattedText = '<div style="font-size:12px">%s</div>' % '<br>'.join(lines)

    def _update_result(self) -> None:
        box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_RESULT))
        have = self.point is not None and self.frame is not None
        for bid in (IN_COPY_XYZ, IN_COPY_GCODE, IN_CREATE_POINT):
            self.inputs.itemById(bid).isEnabled = have
        if not have:
            box.formattedText = ('<div style="color:%s;font-size:12px">Click a point on the stock or model.<br>'
                                 'X, Y, Z appear here in the setup WCS.</div>' % GREY)
            return
        unit = self._display_unit()
        rows = ''.join('<tr><td style="padding-right:10px"><b>%s</b></td><td align="right"><b>%s</b></td>'
                       '<td>&nbsp;%s</td></tr>' % (line[0], line[3:].rsplit(' ', 1)[0].strip(), unit)
                       for line in self.point.formatted_lines(unit))
        note = 'on %s' % self.point.source_label()
        if self.point.is_approximate():
            note += ' &middot; <span style="color:%s">simulation-resolution accuracy</span>' % GREY
        box.formattedText = ('<table style="font-family:Consolas,monospace;font-size:15px">%s</table>'
                             '<div style="font-size:11px">%s</div>' % (rows, note))

    def redraw(self) -> None:
        """Draw viewport graphics (executePreview only; Fusion discards graphics made elsewhere)."""
        show = adsk.core.BoolValueCommandInput.cast(self.inputs.itemById(IN_SHOW_TRIAD))
        if self.frame is not None and show and show.value:
            self.markers.show_triad(self.frame)
        else:
            self.markers.clear_triad()
        if self.point is None:
            self.markers.clear_point()
        else:
            unit = self._display_unit()
            label = ' '.join(l.replace('  ', ' ') for l in self.point.formatted_lines(unit))
            self.markers.show_point(self.point.world_xyz_mm, label)

    def _request_redraw(self) -> None:
        try:
            self.command.doExecutePreview()
        except Exception as exc:
            log.error('doExecutePreview failed', exc)

    # ------------------------------------------------------------- events
    def on_input_changed(self, changed: adsk.core.CommandInput) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            cid = changed.id
            if cid == IN_SETUP:
                setup = self.selected_setup()
                self._apply_setup(setup)
                self._update_result()
                if self.stock.current_for(self.doc, setup) is None:
                    self.run_action(PendingAction('acquire', setup=setup))
                else:
                    self._refresh_source()
                    self._apply_pick_filters()
                    self._request_redraw()
            elif cid == IN_PICK:
                self._on_pick(adsk.core.SelectionCommandInput.cast(changed))
            elif cid == IN_UNITS:
                self._update_result()
                self._request_redraw()
            elif cid == IN_SHOW_TRIAD:
                self.prefs.set('show_triad', bool(adsk.core.BoolValueCommandInput.cast(changed).value))
                self._request_redraw()
            elif cid == IN_PICK_MODEL:
                self._apply_pick_filters()
            elif cid == IN_DIAG_LOG:
                value = bool(adsk.core.BoolValueCommandInput.cast(changed).value)
                self.prefs.set('diagnostics', value)
                log.set_enabled(value)
            elif cid == IN_ADVANCED:
                self.prefs.set('advanced_expanded', bool(adsk.core.GroupCommandInput.cast(changed).isExpanded))
            elif cid in (IN_COPY_XYZ, IN_COPY_GCODE, IN_CREATE_POINT, IN_REFRESH, IN_LOAD_STOCK, IN_REMOVE_STOCK, IN_SELF_TEST):
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
            self._copy(self.point.clipboard_text(self._display_unit()))
        elif cid == IN_COPY_GCODE:
            self._copy(self.point.machine_text(self._display_unit()))
        elif cid == IN_CREATE_POINT:
            name = 'IPW %s %s' % (self.point.setup_name, self.point.machine_text(self._display_unit()))
            self.run_action(PendingAction('reference_point', design=self.design,
                                          world_xyz_mm=self.point.world_xyz_mm, name=name))
        elif cid == IN_REFRESH:
            self.run_action(PendingAction('acquire', setup=self.selected_setup()))
        elif cid == IN_LOAD_STOCK:
            self._load_stock()
        elif cid == IN_REMOVE_STOCK:
            self.run_action(PendingAction('remove_mesh'))
        elif cid == IN_SELF_TEST and self.owner.self_test is not None:
            self.owner.self_test.run_and_show(self.doc, self.selected_setup(), self.result)

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
        if self.frame is None:
            pick.clearSelection()
            return
        kind, name = self._classify(entity)
        self.point = InspectedPoint.from_world(world_mm, self.frame, kind, name)
        log.info('picked %s "%s" world %s mm -> %s %s mm' % (kind, name, _r(world_mm), self.point.setup_name,
                                                             _r(self.point.setup_xyz_mm)))
        self._update_result()
        pick.clearSelection()      # the next click is a new pick; the marker stays
        pick.hasFocus = True
        self._request_redraw()

    def _classify(self, entity) -> tuple:
        try:
            if adsk.fusion.MeshBody.cast(entity):
                if self.stock.is_temporary_entity(self.doc, entity):
                    return ('ipw', entity.name)
                return ('mesh', entity.name)
            if adsk.fusion.BRepBody.cast(entity) or adsk.fusion.BRepFace.cast(entity) or adsk.fusion.BRepEdge.cast(entity):
                body = getattr(entity, 'body', entity)
                return ('model', getattr(body, 'name', ''))
            if adsk.fusion.BRepVertex.cast(entity) or adsk.fusion.SketchPoint.cast(entity) or \
                    adsk.fusion.ConstructionPoint.cast(entity):
                return ('point', getattr(entity, 'name', ''))
        except Exception:
            pass
        return ('model', '')

    def _copy(self, text: str) -> None:
        ok = clipboard.copy_text(text)
        self._status = 'Copied: %s' % text.replace('\t', '  ') if ok else 'Could not access the clipboard.'
        self._refresh_source()

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
        self.run_action(PendingAction('load_saved', setup=self.selected_setup(), path=path))

    def run_action(self, action: PendingAction) -> None:
        """Hand a document change to the action command; the dialog closes and reopens afterwards."""
        adv = adsk.core.GroupCommandInput.cast(self.inputs.itemById(IN_ADVANCED))
        allow_model = adsk.core.BoolValueCommandInput.cast(self.inputs.itemById(IN_PICK_MODEL))
        self.owner.resume = {
            'setup_name': (action.payload.get('setup') or self.selected_setup()).name,
            'point': self.point,
            'units': self._display_unit() if self._display_unit() != self.unit else None,
            'advanced': bool(adv.isExpanded) if adv else False,
            'pick_model': bool(allow_model.value) if allow_model else False,
        }
        self.owner.actions.run(action)

    def close(self) -> None:
        self.owner.stop_watcher()
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
                                   (command.executePreview, _PreviewHandler(session)),
                                   (command.destroy, _DestroyHandler(session)),
                                   (command.execute, _NoopHandler())):
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


class _PreviewHandler(adsk.core.CommandEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        try:
            self.session.redraw()
        except Exception as exc:
            log.error('redraw failed', exc)


class _NoopHandler(adsk.core.CommandEventHandler):
    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        pass


class _DestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        self.session.close()


class _NewFileHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.on_new_file(args.additionalInfo)
        except Exception as exc:
            log.error('handling a new stock file failed', exc)


# ------------------------------------------------------------------ helpers
def _button(inputs: adsk.core.CommandInputs, input_id: str, text: str, tooltip: str) -> adsk.core.BoolValueCommandInput:
    button = inputs.addBoolValueInput(input_id, text, False, '', False)
    button.text = text
    button.isFullWidth = True
    button.tooltip = tooltip
    return button


def _model_size_mm(design: adsk.fusion.Design) -> float:
    """Diagonal of the design's visible geometry (mm), used to scale viewport graphics."""
    try:
        box = None
        for item in list(design.rootComponent.bRepBodies) + [o for o in design.rootComponent.allOccurrences]:
            bb = item.boundingBox
            cur = [bb.minPoint.x, bb.minPoint.y, bb.minPoint.z, bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z]
            box = cur if box is None else [min(box[0], cur[0]), min(box[1], cur[1]), min(box[2], cur[2]),
                                           max(box[3], cur[3]), max(box[4], cur[4]), max(box[5], cur[5])]
        if box is None:
            return 50.0
        return max(1.0, ((box[3] - box[0]) ** 2 + (box[4] - box[1]) ** 2 + (box[5] - box[2]) ** 2) ** 0.5 * 10.0)
    except Exception:
        return 50.0


def _r(v) -> str:
    return '(%.4f, %.4f, %.4f)' % tuple(v)
