"""The "IPW Inspector" dialog.

    Setup        [Setup5 v]
    ● Current IPW · Setup5 · Ready
    Pick point   [Select]
    ● CORNER
    X   +2.078 mm
    Y  -25.150 mm
    Z  +22.000 mm
    3-plane intersection · residual 0.000 mm · high confidence
    [Copy XYZ]  [Copy G-code]
    > Advanced

Hovering the in-process stock previews the feature that a click will pick
(corner, edge, surface or raw point; N cycles the candidates). Reading a
point never changes the document. Everything that does (getting the stock,
loading a file, creating a reference point) goes through ``actions_command``
and the dialog reopens where it was. Closing the dialog removes the temporary
stock again.
"""
from __future__ import annotations

import os
import time
import traceback
from typing import List, Optional

import adsk.cam
import adsk.core
import adsk.fusion

from ..core.point_inspector import InspectedPoint, normalize_unit
from ..core.transform import SetupFrame
from ..diagnostics import log
from ..stock.acquisition import StockSession, StockSessions
from ..stock.provider import SOURCE_CURRENT, StockResult
from ..stock.saved_file import FolderWatcher
from ..ui.marker import Markers, sweep_orphans
from ..utils import clipboard
from ..utils.fusion_units import api_point_to_mm, document_length_unit
from ..utils.prefs import Preferences
from .actions_command import ActionCommand, PendingAction
from .snap_controller import SNAP_READY_EVENT_ID, IndexCache, SnapController

COMMAND_ID = 'FusionIPWInspector_Inspect'
COMMAND_NAME = 'IPW Inspector'
COMMAND_TOOLTIP = 'Read X, Y, Z of corners, edges and surfaces of the in-process stock in a Setup WCS.'
RESOURCES = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources', 'ipw_inspector')
STOCK_FILTER = 'Saved stock (*.stl);;All files (*.*)'
NEW_FILE_EVENT_ID = 'FusionIPWInspector_NewStockFile'

IN_SETUP = 'setup'
IN_STATUS = 'status'
IN_PICK = 'pick'
IN_RESULT = 'result'
IN_COPY_XYZ = 'copy_xyz'
IN_COPY_GCODE = 'copy_gcode'
IN_ADVANCED = 'advanced'
IN_UNITS = 'units'
IN_SHOW_TRIAD = 'show_triad'
IN_SHOW_IPW = 'show_ipw'
IN_PICK_MODEL = 'pick_model'
IN_CREATE_POINT = 'create_point'
IN_REFRESH = 'refresh'
IN_LOAD_STOCK = 'load_stock'
IN_DIAG_LOG = 'diag_log'
IN_SELF_TEST = 'self_test'
IN_DETAILS = 'details'

MODEL_FILTERS = ('MeshBodies', 'SolidBodies', 'SurfaceBodies', 'Faces', 'Edges', 'Vertices', 'SketchPoints', 'ConstructionPoints')
GREY = '#8a8a8a'
FEATURE_GLYPH = {'corner': '&#9679;', 'edge': '&#9644;', 'surface': '&#9635;', 'raw': '&#183;'}

CLEANUP_EVENT_ID = 'FusionIPWInspector_Cleanup'
CLICK_EVENT_ID = 'FusionIPWInspector_Click'
REFRESH_EVENT_ID = 'FusionIPWInspector_Refresh'
VISIBILITY_EVENT_ID = 'FusionIPWInspector_Visibility'
CLICK_MAX_DRAG_PX = 4.0
MODEL_PICK_GRACE_S = 0.3
CYCLE_KEYS = (ord('N'), adsk.core.KeyCodes.TabKeyCode)   # next snap candidate
# Developer aid: when this flag file exists, every hover hit is written to the diagnostics log.
_HOVER_DEBUG = os.path.isfile(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'FusionIPWInspector', 'hover_debug'))

_handlers: List[adsk.core.EventHandler] = []


class InspectorCommand:
    """Owns the command definitions, the shared snap-index cache and the per-dialog session."""

    def __init__(self, app: adsk.core.Application, prefs: Preferences, sessions: StockSessions,
                 self_test: Optional[object] = None) -> None:
        self.app = app
        self.ui = app.userInterface
        self.prefs = prefs
        self.sessions = sessions
        self.self_test = self_test
        self.session: Optional['_Session'] = None
        self.resume: Optional[dict] = None
        self.reopening = False
        self.index_cache = IndexCache()
        self.actions = ActionCommand(app, sessions, self._reopen, self._pick_setup)
        self._definition: Optional[adsk.core.CommandDefinition] = None
        self._doc_handler = None
        self.defer_removal = False              # set while a session is ended by a document switch
        self.pending_cleanup: set = set()       # document names whose temporary stock is removed on re-activation
        self.watcher: Optional[FolderWatcher] = None

    # ------------------------------------------------------------ lifecycle
    def register(self) -> adsk.core.CommandDefinition:
        definitions = self.ui.commandDefinitions
        existing = definitions.itemById(COMMAND_ID)
        if existing:
            existing.deleteMe()
        self._definition = definitions.addButtonDefinition(COMMAND_ID, COMMAND_NAME, COMMAND_TOOLTIP, RESOURCES)
        try:
            self._definition.controlDefinition.isVisible = False   # opened by the toolbar command only
        except Exception:
            pass
        created = _CommandCreatedHandler(self)
        self._definition.commandCreated.add(created)
        _handlers.append(created)
        for event_id, handler in ((NEW_FILE_EVENT_ID, _NewFileHandler(self)), (SNAP_READY_EVENT_ID, _SnapReadyHandler(self)),
                                  (CLEANUP_EVENT_ID, _CleanupHandler(self)), (CLICK_EVENT_ID, _ClickHandler(self)),
                                  (REFRESH_EVENT_ID, _RefreshHandler(self)), (VISIBILITY_EVENT_ID, _VisibilityHandler(self))):
            try:
                self.app.unregisterCustomEvent(event_id)
            except Exception:
                pass
            event = self.app.registerCustomEvent(event_id)
            event.add(handler)
            _handlers.append(handler)
        activated = _DocumentActivatedHandler(self)
        self.app.documentActivated.add(activated)
        _handlers.append(activated)
        self._doc_handler = activated
        open_definition = self.actions.register(COMMAND_NAME, COMMAND_TOOLTIP, RESOURCES)
        open_definition.toolClipFilename = os.path.join(RESOURCES, '64x64.png')
        return open_definition

    def unregister(self) -> None:
        self.stop_watcher()
        if self.session is not None:
            self.session.close(remove_stock=True)
        if self._doc_handler is not None:
            try:
                self.app.documentActivated.remove(self._doc_handler)
            except Exception:
                pass
            self._doc_handler = None
        sweep_orphans(self.app)
        for event_id in (NEW_FILE_EVENT_ID, SNAP_READY_EVENT_ID, CLEANUP_EVENT_ID, CLICK_EVENT_ID, REFRESH_EVENT_ID,
                         VISIBILITY_EVENT_ID):
            try:
                self.app.unregisterCustomEvent(event_id)
            except Exception:
                pass
        self.actions.unregister()
        if self._definition:
            try:
                self._definition.deleteMe()
            except Exception:
                pass
            self._definition = None
        self.index_cache.clear()
        _handlers.clear()

    def _reopen(self) -> None:
        self.reopening = False
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
        session = self.session
        if session is None or (session.result is not None and session.result.source == SOURCE_CURRENT):
            return
        log.info('new stock file detected: %s' % path)
        session.run_action(PendingAction('load_saved', setup=session.selected_setup(), path=path))

    def on_snap_ready(self, payload: str) -> None:
        session = self.session
        if session is not None:
            fingerprint, _, stage = payload.partition('|')
            session.on_snap_ready(fingerprint, stage or 'index')

    def on_click_event(self) -> None:
        session = self.session
        if session is not None:
            session.on_click_deferred()

    def on_visibility_event(self, payload: str) -> None:
        session = self.session
        if session is not None:
            session.apply_ipw_visibility(payload == '1')

    def on_refresh_event(self) -> None:
        session = self.session
        if session is not None and session.markers is not None:
            session.markers.deferred_refresh()

    def on_document_activated(self, doc: adsk.core.Document) -> None:
        """Another document came to the front while the inspector was open: end the session.

        Fusion keeps the command alive (hidden) with its document. Deleting the
        temporary stock from a document that is not active does not stick, so the
        session is closed without removal and the stock is removed the moment its
        document becomes active again (or before it is saved, or when the add-in
        stops).
        """
        if doc is None:
            return
        try:
            name = doc.name
        except Exception:
            return
        if name in self.pending_cleanup:
            # Deleting inside the activation event does not stick; do it once Fusion is idle.
            try:
                self.app.fireCustomEvent(CLEANUP_EVENT_ID, '')
            except Exception:
                pass
        session = self.session
        if session is None or session.doc is None:
            return
        try:
            same = name == session.doc.name
        except Exception:
            same = True
        if same:
            return
        log.info('document changed to %s while the inspector was open: closing it' % name)
        try:
            self.pending_cleanup.add(session.doc.name)
        except Exception:
            pass
        self.defer_removal = True
        try:
            self.ui.terminateActiveCommand()
        except Exception:
            pass
        if self.session is not None:          # the command was not the active one: close directly
            self.session.close(remove_stock=False)
        self.defer_removal = False

    def on_cleanup(self) -> None:
        """Runs once Fusion is idle after a dialog closed or a document was re-activated.

        No command is active here, so deletions stick: leftover graphics are
        swept and temporary stock whose removal had to wait for its document to
        become active again is removed now.
        """
        if self.session is None:
            sweep_orphans(self.app)
        doc = self.app.activeDocument
        try:
            name = doc.name if doc is not None else ''
        except Exception:
            name = ''
        if name and name in self.pending_cleanup and (self.session is None or self.session.doc is None
                                                       or self.session.doc.name != name):
            self.pending_cleanup.discard(name)
            try:
                removed = self.sessions.for_document(doc).remove_mesh(doc)
                log.info('temporary stock removed from %s on re-activation (%d)' % (name, removed))
            except Exception as exc:
                log.error('deferred cleanup failed', exc)


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
        self.snap = SnapController(owner.app, owner.index_cache)
        self.unit = 'mm'
        self._busy = False
        self._notice = ''        # short transient message, e.g. "XYZ copied"
        self._closed = False
        self._mouse_down: Optional[tuple] = None
        self._model_pick_at = 0.0
        self._ipw_visible = True
        self._built_at = time.perf_counter()

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
        sweep_orphans(self.app, self.doc)
        self.markers = Markers(self._graphics_groups(), _model_size_mm(self.design))
        self.markers.refresh_on_preview = not os.path.isfile(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'FusionIPWInspector', 'no_refresh'))
        self.markers.schedule_refresh = lambda: self.app.fireCustomEvent(REFRESH_EVENT_ID, '')
        log.set_enabled(bool(self.prefs.get('diagnostics')))
        resume = self.owner.resume or {}
        self.owner.resume = None
        self._notice = self.owner.actions.last_status
        self.owner.actions.last_status = ''

        cmd = self.command
        cmd.isOKButtonVisible = False
        cmd.cancelButtonText = 'Close'
        cmd.isRepeatable = False
        cmd.setDialogInitialSize(320, 400)
        cmd.setDialogMinimumSize(300, 360)

        inputs = self.inputs
        setup = self._initial_setup(resume.get('setup_name'))
        dd = inputs.addDropDownCommandInput(IN_SETUP, 'Setup', adsk.core.DropDownStyles.TextListDropDownStyle)
        for s in self.setups:
            dd.listItems.add(s.name, s is setup)
        dd.tooltip = 'Coordinates are relative to this setup\'s work coordinate system (WCS).'

        status = inputs.addTextBoxCommandInput(IN_STATUS, ' ', '', 2, True)
        status.isFullWidth = True

        self._ipw_visible = bool(resume.get('show_ipw', True))
        show_ipw = inputs.addBoolValueInput(IN_SHOW_IPW, 'Show IPW', True, '', self._ipw_visible)
        show_ipw.tooltip = ('Hide or show the in-process stock overlay. Snapping data stays loaded; '
                            'the model itself is never touched.')

        pick = inputs.addSelectionInput(IN_PICK, 'Pick point', 'Hover the in-process stock: corner, edge or surface. N cycles. Click to pick.')
        pick.setSelectionLimits(0, 1)
        pick.tooltip = 'Move near a corner, edge or surface of the stock; the preview shows what a click picks. Press N to cycle candidates.'

        result = inputs.addTextBoxCommandInput(IN_RESULT, ' ', '', 5, True)
        result.isFullWidth = True

        _button(inputs, IN_COPY_XYZ, 'Copy XYZ', 'Copy the three values, tab separated.')
        _button(inputs, IN_COPY_GCODE, 'Copy G-code', 'Copy as X.. Y.. Z.., for example X12.384 Y-21.750 Z6.125')

        adv = inputs.addGroupCommandInput(IN_ADVANCED, 'Advanced')
        adv.isExpanded = bool(resume.get('advanced', False))
        a = adv.children
        units = a.addDropDownCommandInput(IN_UNITS, 'Units', adsk.core.DropDownStyles.TextListDropDownStyle)
        chosen = resume.get('units')
        units.listItems.add('Document (%s)' % self.unit, chosen not in ('mm', 'in'))
        units.listItems.add('mm', chosen == 'mm')
        units.listItems.add('in', chosen == 'in')
        a.addBoolValueInput(IN_SHOW_TRIAD, 'Show WCS triad', True, '', bool(self.prefs.get('show_triad')))
        a.addBoolValueInput(IN_PICK_MODEL, 'Allow model selection', True, '', bool(resume.get('pick_model', False))).tooltip = \
            'Also pick faces, edges and points of the design model. The reading then says MODEL instead of IPW.'
        _button(a, IN_CREATE_POINT, 'Create reference point',
                'Add a construction point at the picked location, named with its WCS coordinates.')
        _button(a, IN_REFRESH, 'Refresh IPW', 'Ask Fusion again for the in-process stock of the selected setup.')
        _button(a, IN_LOAD_STOCK, 'Import saved stock...',
                'Fallback: use a stock file saved from Simulation (right-click the stock > Stock > Save Stock...).')
        a.addBoolValueInput(IN_DIAG_LOG, 'Diagnostics log', True, '', bool(self.prefs.get('diagnostics'))).tooltip = \
            'Log file: ' + log.log_path()
        _button(a, IN_SELF_TEST, 'Run self test', 'Check the transform math, units, setup WCS and the loaded stock.')
        details = a.addTextBoxCommandInput(IN_DETAILS, ' ', '', 6, True)
        details.isFullWidth = True

        self._apply_setup(setup)
        if resume.get('point') is not None and self.frame is not None:
            self.point = resume['point'].rebased(self.frame)
        self._refresh_stock_state()
        self._update_result()
        self._update_details()
        self._apply_pick_filters()
        pick.hasFocus = True
        log.info('dialog built in %.3f s' % (time.perf_counter() - self._built_at))
        return True

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
        if self.point is not None and self.frame is not None:
            self.point = self.point.rebased(self.frame)

    def _display_unit(self) -> str:
        dd = adsk.core.DropDownCommandInput.cast(self.inputs.itemById(IN_UNITS))
        if dd and dd.selectedItem and dd.selectedItem.name in ('mm', 'in'):
            return dd.selectedItem.name
        return self.unit

    def _refresh_stock_state(self) -> None:
        """Re-read what is loaded for the selected setup and attach the snap index."""
        setup = self.selected_setup()
        self.result = self.stock.current_for(self.doc, setup)
        if self.result is not None and self.result.frame is not None:
            self.snap.attach(self.result.fingerprint, self.result.stock_path, self.result.unit_scale_mm, self.result.frame)
            self.owner.stop_watcher()
            self._configure_mesh()
        else:
            self.snap.detach()
            folder = self.prefs.get('last_stock_folder') or ''
            if folder:
                self.owner.start_watcher(folder)
        self._update_status()

    def _configure_mesh(self) -> None:
        """The stock overlay is display only: hovering and clicking go through our own ray cast,
        so Fusion never hit-tests or highlights the 829k-triangle body."""
        try:
            occ, mesh = self.stock.mesh_occurrence(self.doc)
            if mesh is not None:
                mesh.isSelectable = False
            if occ is not None and occ.isLightBulbOn != self._ipw_visible:
                occ.isLightBulbOn = self._ipw_visible
        except Exception as exc:
            log.error('configuring the stock overlay failed', exc)

    def _set_ipw_visible(self, visible: bool) -> None:
        """Show IPW checkbox: visibility only; nothing is exported, imported or re-indexed.

        The light bulb is switched from an idle event: a document change made
        inside the dialog's own inputChanged handler does not stick.
        """
        self._ipw_visible = visible
        if not visible and self.snap.preview is not None:
            self.snap.end_hover()
            self._draw_preview()
        try:
            self.app.fireCustomEvent(VISIBILITY_EVENT_ID, '1' if visible else '0')
        except Exception as exc:
            log.error('scheduling the overlay visibility change failed', exc)

    def apply_ipw_visibility(self, visible: bool) -> None:
        t0 = time.perf_counter()
        try:
            occ, _ = self.stock.mesh_occurrence(self.doc)
            if occ is not None:
                occ.isLightBulbOn = visible
                self.app.activeViewport.refresh()
                log.info('Show IPW %s in %.1f ms (light bulb now %s)' % (
                    'on' if visible else 'off', (time.perf_counter() - t0) * 1000.0, occ.isLightBulbOn))
        except Exception as exc:
            log.error('toggling the stock overlay failed', exc)

    def _update_status(self) -> None:
        box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_STATUS))
        setup = self.selected_setup()
        last = self.owner.actions.last_result
        if self.result is not None:
            title = 'Current IPW' if self.result.source == SOURCE_CURRENT else 'Saved IPW'
            state = {'ready': 'Ready', 'preparing': 'Preparing IPW&hellip;', 'none': 'Ready'}[self.snap.state]
            if self.result.is_plain_box:
                state = 'Unmachined box'
            line = '<b>&#9679; %s</b> &nbsp;&middot;&nbsp; %s &nbsp;&middot;&nbsp; %s' % (title, setup.name, state)
        else:
            reason = ''
            if last is not None and last.setup_id == setup.operationId and last.error:
                reason = last.error
            line = '<b>&#9888; No IPW available</b> &nbsp;&middot;&nbsp; %s' % setup.name
            if reason:
                line += '<br><span style="color:%s">%s</span>' % (GREY, reason)
            else:
                line += '<br><span style="color:%s">Model geometry can still be picked.</span>' % GREY
        if self._notice:
            line += '<br><span style="color:%s">%s</span>' % (GREY, self._notice)
        box.formattedText = '<div style="font-size:12px">%s</div>' % line

    def _update_result(self) -> None:
        box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_RESULT))
        have = self.point is not None and self.frame is not None
        for bid in (IN_COPY_XYZ, IN_COPY_GCODE, IN_CREATE_POINT):
            self.inputs.itemById(bid).isEnabled = have
        if not have:
            box.formattedText = ('<div style="font-size:12px">No point selected<br>'
                                 '<span style="color:%s">Move near a corner, edge or surface, then click.</span></div>' % GREY)
            return
        unit = self._display_unit()
        p = self.point
        source = 'MODEL' if p.source_kind in ('model', 'point') else 'IPW'
        head = '<b>%s %s</b> <span style="color:%s">&middot; %s</span>' % (FEATURE_GLYPH.get(p.feature, '&#183;'), p.feature_label(), GREY, source)
        rows = ''.join('<tr><td style="padding-right:10px"><b>%s</b></td><td align="right"><b>%s</b></td>'
                       '<td>&nbsp;%s</td></tr>' % (line[0], line[3:].rsplit(' ', 1)[0].strip(), unit)
                       for line in p.formatted_lines(unit))
        if p.method:
            tail = '%s &middot; residual %.3f mm &middot; %s' % (p.method, p.residual_mm, p.confidence)
        else:
            tail = 'exact model geometry' if source == 'MODEL' else 'mesh hit'
        box.formattedText = ('<div style="font-size:12px">%s</div><table style="font-family:Consolas,monospace;font-size:15px">%s</table>'
                             '<div style="font-size:11px;color:%s">%s</div>' % (head, rows, GREY, tail))

    def _update_details(self) -> None:
        box = adsk.core.TextBoxCommandInput.cast(self.inputs.itemById(IN_DETAILS))
        setup = self.selected_setup()
        lines = []
        if self.frame is not None:
            lines.append('%s WCS: %s' % (setup.name, self.frame.describe()))
        else:
            lines.append('The WCS of %s could not be read. Open the setup and check its WCS.' % setup.name)
        if self.point is not None:
            lines.append('World: (%.4f, %.4f, %.4f) mm' % self.point.world_xyz_mm)
        if self.result is not None:
            lines.append('Stock: %d triangles, %.2f mm mean edge, %s' % (self.result.triangle_count, self.result.mean_edge_mm, self.result.provider))
        lines.append(self.snap.diagnostics())
        box.text = '\n'.join(lines)

    def _apply_pick_filters(self) -> None:
        pick = adsk.core.SelectionCommandInput.cast(self.inputs.itemById(IN_PICK))
        allow_model = adsk.core.BoolValueCommandInput.cast(self.inputs.itemById(IN_PICK_MODEL))
        model_ok = bool(allow_model and allow_model.value) or self.result is None or not self.result.mesh_body_valid
        pick.clearSelectionFilter()
        for f in (MODEL_FILTERS if model_ok else ('MeshBodies',)):
            pick.addSelectionFilter(f)

    # ------------------------------------------------------------ drawing
    def redraw(self) -> None:
        """Draw all viewport graphics (executePreview only; Fusion discards the previous preview)."""
        self.markers.begin()
        show = adsk.core.BoolValueCommandInput.cast(self.inputs.itemById(IN_SHOW_TRIAD))
        if self.frame is not None and show and show.value:
            self.markers.show_triad(self.frame)
        if self.point is not None:
            self.markers.show_point(self.point.feature or 'raw', self.point.world_xyz_mm, None,
                                    self.point.feature_label())
        self._draw_preview()

    def _draw_preview(self) -> None:
        """Move (or hide) the persistent hover marker directly; never through executePreview."""
        pv = self.snap.preview
        if pv is None:
            self.markers.clear_preview()
            return
        label = pv.candidate.label
        if pv.cycle_note:
            label += ' ' + pv.cycle_note
        self.markers.show_preview(pv.candidate.kind, pv.world_mm, pv.direction_world, label,
                                  size_mm=max(self.markers.feature_size_mm, pv.radius_mm * 2.0))

    def _request_redraw(self) -> None:
        try:
            self.command.doExecutePreview()
        except Exception as exc:
            log.error('doExecutePreview failed', exc)

    # ------------------------------------------------------------- events
    def on_mouse_move(self, args: adsk.core.MouseEventArgs) -> None:
        """Hover: ray cast on the analysis mesh, snap, move the persistent marker."""
        if self._closed or not self._ipw_visible or self.snap.index is None:
            return
        prof = self.snap.profile
        t0 = time.perf_counter()
        try:
            pos = args.viewportPosition
            preview = self.snap.hover_view(args.viewport, pos.x, pos.y)
            t1 = time.perf_counter()
            if _HOVER_DEBUG and preview is not None and preview.changed:
                hit = self.snap.last_hit[0]
                c = preview.candidate
                log.info('hover view (%.0f, %.0f) hit %s -> %s %s d=%.3f R=%.3f key=%s' % (
                    pos.x, pos.y, _r(hit), c.label, _r(c.point), c.distance_from_hit, preview.radius_mm, c.key))
            if preview is None:
                if self.snap.preview is not None:
                    self.snap.end_hover()
                    self._draw_preview()
                return
            if preview.changed:
                self._draw_preview()
                t2 = time.perf_counter()
                prof.add('graphics', (t2 - t1) * 1000.0 - self.markers.last_refresh_ms)
                prof.add('refresh', self.markers.last_refresh_ms)
                prof.add('total', (t2 - t0) * 1000.0)
        except Exception as exc:
            log.error('hover failed', exc)

    def on_mouse_down(self, args: adsk.core.MouseEventArgs) -> None:
        try:
            if args.button == adsk.core.MouseButtons.LeftMouseButton:
                pos = args.viewportPosition
                self._mouse_down = (pos.x, pos.y)
        except Exception:
            self._mouse_down = None

    def on_mouse_up(self, args: adsk.core.MouseEventArgs) -> None:
        """A click (no drag) on the stock picks the previewed candidate; deferred to an idle
        event so Fusion has finished its own selection handling first."""
        try:
            if args.button != adsk.core.MouseButtons.LeftMouseButton or self._mouse_down is None:
                return
            pos = args.viewportPosition
            dx, dy = pos.x - self._mouse_down[0], pos.y - self._mouse_down[1]
            self._mouse_down = None
            if dx * dx + dy * dy > CLICK_MAX_DRAG_PX * CLICK_MAX_DRAG_PX:
                return
            if self.snap.preview is None or not self._ipw_visible:
                return
            self.app.fireCustomEvent(CLICK_EVENT_ID, '')
        except Exception as exc:
            log.error('click handling failed', exc)

    def on_click_deferred(self) -> None:
        if self._closed or self.frame is None:
            return
        if time.perf_counter() - self._model_pick_at < MODEL_PICK_GRACE_S:
            return                      # Fusion selected model geometry for this click
        committed = self.snap.commit_current()
        if committed is None:
            return
        candidate, world_mm = committed
        self.point = InspectedPoint.from_world(world_mm, self.frame, 'ipw', 'In-process stock', candidate.kind,
                                               candidate.method(), candidate.residual, candidate.confidence())
        log.info('picked ipw %s world %s mm -> %s %s mm (%s, residual %.4f, cond %.2f, from hit %.3f mm)' % (
            candidate.kind, _r(world_mm), self.point.setup_name, _r(self.point.setup_xyz_mm),
            candidate.method(), candidate.residual, candidate.conditioning, candidate.distance_from_hit))
        self._notice = ''
        self._update_result()
        self._update_details()
        self._update_status()
        self._request_redraw()

    def on_hover_end(self) -> None:
        if self.snap.preview is not None:
            self.snap.end_hover()
            self._draw_preview()

    def on_key(self, key_code: int) -> None:
        # Tab only reaches the command while the viewport owns keyboard focus (the dialog
        # consumes it for focus traversal), so a plain letter is the documented key.
        if key_code in CYCLE_KEYS and self.snap.preview is not None:
            if self.snap.cycle() is not None:
                self._draw_preview()

    def on_snap_ready(self, fingerprint: str, stage: str) -> None:
        if self._closed or fingerprint != self.snap.fingerprint:
            return
        if self.snap.refresh_ready():
            self._update_status()
            self._update_details()

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
                    self._notice = ''
                    self.run_action(PendingAction('acquire', setup=setup))
                else:
                    self._refresh_stock_state()
                    self._update_details()
                    self._apply_pick_filters()
                    self._request_redraw()
            elif cid == IN_PICK:
                self._on_pick(adsk.core.SelectionCommandInput.cast(changed))
            elif cid == IN_UNITS:
                self._update_result()
                self._update_details()
                self._request_redraw()
            elif cid == IN_SHOW_IPW:
                self._set_ipw_visible(bool(adsk.core.BoolValueCommandInput.cast(changed).value))
            elif cid == IN_SHOW_TRIAD:
                self.prefs.set('show_triad', bool(adsk.core.BoolValueCommandInput.cast(changed).value))
                self._request_redraw()
            elif cid == IN_PICK_MODEL:
                self._apply_pick_filters()
            elif cid == IN_DIAG_LOG:
                value = bool(adsk.core.BoolValueCommandInput.cast(changed).value)
                self.prefs.set('diagnostics', value)
                log.set_enabled(value)
            elif cid in (IN_COPY_XYZ, IN_COPY_GCODE, IN_CREATE_POINT, IN_REFRESH, IN_LOAD_STOCK, IN_SELF_TEST):
                button = adsk.core.BoolValueCommandInput.cast(changed)
                if button.value:
                    button.value = False
                    self._run_button(cid)
        except Exception as exc:
            log.error('input change failed', exc)
            self._notice = 'Something went wrong; details are in the diagnostics log.'
            self._update_status()
        finally:
            self._busy = False

    def _run_button(self, cid: str) -> None:
        if cid == IN_COPY_XYZ:
            self._copy(self.point.clipboard_text(self._display_unit()), 'XYZ copied')
        elif cid == IN_COPY_GCODE:
            self._copy(self.point.machine_text(self._display_unit()), 'G-code copied')
        elif cid == IN_CREATE_POINT:
            name = 'IPW %s %s' % (self.point.setup_name, self.point.machine_text(self._display_unit()))
            self.run_action(PendingAction('reference_point', design=self.design,
                                          world_xyz_mm=self.point.world_xyz_mm, name=name))
        elif cid == IN_REFRESH:
            self.run_action(PendingAction('acquire', setup=self.selected_setup()))
        elif cid == IN_LOAD_STOCK:
            self._load_stock()
        elif cid == IN_SELF_TEST and self.owner.self_test is not None:
            self.owner.self_test.run_and_show(self.doc, self.selected_setup(), self.result)

    def _on_pick(self, pick: adsk.core.SelectionCommandInput) -> None:
        if pick.selectionCount == 0:
            return
        selection = pick.selection(0)
        entity = selection.entity
        try:
            hit_point = selection.point
        except Exception:
            pick.clearSelection()
            return
        if self.frame is None:
            pick.clearSelection()
            return
        kind, name = self._classify(entity)
        self._model_pick_at = time.perf_counter()
        if kind == 'ipw':
            candidate, world_mm = self.snap.commit(hit_point)
            self.point = InspectedPoint.from_world(world_mm, self.frame, kind, name, candidate.kind, candidate.method(),
                                                   candidate.residual, candidate.confidence())
            log.info('picked %s %s world %s mm -> %s %s mm (%s, residual %.4f, cond %.2f, from hit %.3f mm)' % (
                kind, candidate.kind, _r(world_mm), self.point.setup_name, _r(self.point.setup_xyz_mm),
                candidate.method(), candidate.residual, candidate.conditioning, candidate.distance_from_hit))
        else:
            world_mm = api_point_to_mm(hit_point)
            self.point = InspectedPoint.from_world(world_mm, self.frame, kind, name)
            log.info('picked %s "%s" world %s mm -> %s %s mm' % (kind, name, _r(world_mm), self.point.setup_name, _r(self.point.setup_xyz_mm)))
        self._notice = ''
        self.snap.end_hover()
        self._update_result()
        self._update_details()
        self._update_status()
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

    def _copy(self, text: str, notice: str) -> None:
        ok = clipboard.copy_text(text)
        self._notice = '%s: %s' % (notice, text.replace('\t', '  ')) if ok else 'Could not access the clipboard.'
        self._update_status()

    def _load_stock(self) -> None:
        dialog = self.ui.createFileDialog()
        dialog.title = 'Import stock saved from Simulation'
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
            'show_ipw': self._ipw_visible,
        }
        self.owner.reopening = True
        self.owner.actions.run(action)

    def close(self, remove_stock: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self.owner.stop_watcher()
        if self.snap.query_times:
            log.info(self.snap.diagnostics())
            log.info(self.snap.profile.summary())
        try:
            if self.markers:
                self.markers.clear()
        except Exception:
            pass
        self.snap.detach()
        try:
            self.app.fireCustomEvent(CLEANUP_EVENT_ID, '')
        except Exception:
            pass
        if remove_stock and self.stock is not None and self.doc is not None:
            try:
                removed = self.stock.remove_mesh(self.doc)
                if removed:
                    log.info('temporary stock removed on close')
            except Exception as exc:
                log.error('removing the temporary stock on close failed', exc)
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
                                   (command.preSelect, _PreSelectHandler(session)),
                                   (command.keyDown, _KeyHandler(session)),
                                   (command.mouseMove, _MouseMoveHandler(session)),
                                   (command.mouseDown, _MouseDownHandler(session)),
                                   (command.mouseUp, _MouseUpHandler(session)),
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


class _PreSelectHandler(adsk.core.SelectionEventHandler):
    """Fired for entities under the cursor: model geometry may be selected, the stock overlay never.

    Rejecting the overlay here also stops Fusion from pre-highlighting it on
    every mouse move; hovering and clicking the stock go through the add-in's own
    ray cast instead.
    """

    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.SelectionEventArgs) -> None:
        try:
            session = self.session
            args.isSelectable = not session.stock.is_temporary_entity(session.doc, args.selection.entity)
        except Exception:
            pass


class _MouseMoveHandler(adsk.core.MouseEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.MouseEventArgs) -> None:
        self.session.on_mouse_move(args)


class _MouseDownHandler(adsk.core.MouseEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.MouseEventArgs) -> None:
        self.session.on_mouse_down(args)


class _MouseUpHandler(adsk.core.MouseEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.MouseEventArgs) -> None:
        self.session.on_mouse_up(args)


class _KeyHandler(adsk.core.KeyboardEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.KeyboardEventArgs) -> None:
        try:
            self.session.on_key(args.keyCode)
        except Exception as exc:
            log.error('key handling failed', exc)


class _NoopHandler(adsk.core.CommandEventHandler):
    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        pass


class _DestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        owner = self.session.owner
        self.session.close(remove_stock=not owner.reopening and not owner.defer_removal)


class _NewFileHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.on_new_file(args.additionalInfo)
        except Exception as exc:
            log.error('handling a new stock file failed', exc)


class _DocumentActivatedHandler(adsk.core.DocumentEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.DocumentEventArgs) -> None:
        try:
            self.owner.on_document_activated(args.document)
        except Exception as exc:
            log.error('document activation handling failed', exc)


class _VisibilityHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.on_visibility_event(args.additionalInfo)
        except Exception as exc:
            log.error('visibility change failed', exc)


class _RefreshHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.on_refresh_event()
        except Exception:
            pass


class _ClickHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.on_click_event()
        except Exception as exc:
            log.error('deferred click failed', exc)


class _CleanupHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.on_cleanup()
        except Exception as exc:
            log.error('graphics cleanup failed', exc)


class _SnapReadyHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: InspectorCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.on_snap_ready(args.additionalInfo)
        except Exception as exc:
            log.error('snap-ready handling failed', exc)


# ------------------------------------------------------------------ helpers
def _button(inputs: adsk.core.CommandInputs, input_id: str, text: str, tooltip: str) -> adsk.core.BoolValueCommandInput:
    button = inputs.addBoolValueInput(input_id, text, False, '', False)
    button.text = text
    button.isFullWidth = True
    button.tooltip = tooltip
    return button


def _model_size_mm(design: adsk.fusion.Design) -> float:
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
