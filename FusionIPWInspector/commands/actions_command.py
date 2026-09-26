"""Document changes requested from the inspector, applied outside of its dialog.

Fusion discards model edits made while another command's dialog is open, so
every change goes through ``ActionCommand``, a dialog-less command:

* ``open``            acquire the in-process stock for a setup, then open the inspector
* ``acquire``         same, requested from the open dialog (setup switch, refresh)
* ``load_saved``      import a stock file saved from Simulation
* ``remove_mesh``     delete the temporary stock component
* ``reference_point`` create a named construction point

Each action is one undo step. When it is done a custom event reopens the
inspector where the user left off (the dialog keeps its state in
``InspectorCommand.resume``).
"""
from __future__ import annotations

import traceback
from typing import Callable, List, Optional

import adsk.cam
import adsk.core
import adsk.fusion

from ..diagnostics import log
from ..stock.acquisition import StockSessions
from ..stock.provider import StockResult
from ..utils.fusion_units import mm_to_api

ACTION_COMMAND_ID = 'FusionIPWInspector_Action'
OPEN_COMMAND_ID = 'FusionIPWInspector_Open'
ACTION_EVENT_ID = 'FusionIPWInspector_RunAction'
REOPEN_EVENT_ID = 'FusionIPWInspector_Reopen'
REFERENCE_SKETCH_NAME = 'IPW Inspector reference points'

_handlers: List[adsk.core.EventHandler] = []


class PendingAction:
    def __init__(self, kind: str, **payload) -> None:
        self.kind = kind
        self.payload = payload

    def __getitem__(self, key):
        return self.payload[key]


class ActionCommand:
    """Runs one PendingAction as its own Fusion command, then reopens the inspector."""

    def __init__(self, app: adsk.core.Application, sessions: StockSessions, reopen: Callable[[], None],
                 pick_setup: Callable[[], Optional[adsk.cam.Setup]]) -> None:
        self.app = app
        self.ui = app.userInterface
        self.sessions = sessions
        self._reopen = reopen
        self._pick_setup = pick_setup
        self._definition: Optional[adsk.core.CommandDefinition] = None
        self._open_definition: Optional[adsk.core.CommandDefinition] = None
        self.pending: Optional[PendingAction] = None
        self.last_status: str = ''
        self.last_result: Optional[StockResult] = None

    # ------------------------------------------------------------ lifecycle
    def register(self, open_name: str, open_tooltip: str, resources: str) -> adsk.core.CommandDefinition:
        definitions = self.ui.commandDefinitions
        for cid in (ACTION_COMMAND_ID, OPEN_COMMAND_ID):
            existing = definitions.itemById(cid)
            if existing:
                existing.deleteMe()
        self._definition = definitions.addButtonDefinition(ACTION_COMMAND_ID, 'IPW Inspector action',
                                                           'Applies an IPW Inspector change to the document.')
        self._open_definition = definitions.addButtonDefinition(OPEN_COMMAND_ID, open_name, open_tooltip, resources)
        for definition, kind in ((self._definition, None), (self._open_definition, 'open')):
            created = _CreatedHandler(self, kind)
            definition.commandCreated.add(created)
            _handlers.append(created)
        for event_id, handler in ((ACTION_EVENT_ID, _DeferredHandler(self)), (REOPEN_EVENT_ID, _ReopenHandler(self))):
            try:
                self.app.unregisterCustomEvent(event_id)
            except Exception:
                pass
            event = self.app.registerCustomEvent(event_id)
            event.add(handler)
            _handlers.append(handler)
        return self._open_definition

    def unregister(self) -> None:
        for event_id in (ACTION_EVENT_ID, REOPEN_EVENT_ID):
            try:
                self.app.unregisterCustomEvent(event_id)
            except Exception:
                pass
        for definition in (self._definition, self._open_definition):
            try:
                if definition:
                    definition.deleteMe()
            except Exception:
                pass
        self._definition = self._open_definition = None
        _handlers.clear()

    # --------------------------------------------------------------- running
    def run(self, action: PendingAction) -> None:
        """Called from the dialog: run ``action`` right after the current event returns."""
        self.pending = action
        self.last_status = ''
        self.app.fireCustomEvent(ACTION_EVENT_ID, action.kind)

    def perform_deferred(self) -> None:
        """Custom event body: no command is active here, so start the action command."""
        if self.pending is None:
            return
        try:
            self.ui.terminateActiveCommand()
        except Exception:
            pass
        self._definition.execute()

    def perform(self, action: PendingAction) -> None:
        """Execute handler body of the action command."""
        doc = self.app.activeDocument
        try:
            if action.kind in ('open', 'acquire'):
                setup = action.payload.get('setup') or self._pick_setup()
                if setup is None:
                    self.last_status = ''
                else:
                    self._acquire(doc, setup)
            elif action.kind == 'load_saved':
                session = self.sessions.for_document(doc)
                result = session.load_saved(doc, action['setup'], action['path'])
                self.last_result = result
                self.last_status = 'Loaded %s.' % action['path'].replace('\\', '/').split('/')[-1] if result.ok \
                    else 'Could not use %s: %s' % (action['path'].replace('\\', '/').split('/')[-1], result.error)
            elif action.kind == 'remove_mesh':
                removed = self.sessions.for_document(doc).remove_mesh(doc)
                self.last_status = 'Temporary stock removed.' if removed else 'There was no temporary stock to remove.'
            elif action.kind == 'reference_point':
                name = _create_reference_point(action['design'], action['world_xyz_mm'], action['name'])
                self.last_status = 'Reference point created: %s' % name
            log.info('action %s done: %s' % (action.kind, self.last_status))
        except Exception as exc:
            log.error('action %s failed' % action.kind, exc)
            self.last_status = 'The action failed: %s' % str(exc).splitlines()[0][:200]
            self.ui.messageBox('IPW Inspector could not complete the action.\n\n%s' % traceback.format_exc(),
                               'IPW Inspector')
        self.app.fireCustomEvent(REOPEN_EVENT_ID, '')

    def _acquire(self, doc: adsk.core.Document, setup: adsk.cam.Setup) -> None:
        session = self.sessions.for_document(doc)
        progress = self.ui.createProgressDialog()
        progress.isCancelButtonShown = False
        progress.isBackgroundTranslucent = False
        try:
            progress.show('IPW Inspector', 'Reading the in-process stock of %s...' % setup.name, 0, 1, 0)
            result = session.acquire_current(doc, setup)
        finally:
            progress.hide()
        self.last_result = result
        self.last_status = ''
        log.info('acquire %s -> %s' % (setup.name, result.label()))

    def reopen_now(self) -> None:
        try:
            self._reopen()
        except Exception as exc:
            log.error('reopening the inspector failed', exc)


def _create_reference_point(design: adsk.fusion.Design, world_xyz_mm, name: str) -> str:
    """Named construction point at a world position (mm).

    ``ConstructionPointInput.setByPoint`` accepts a bare point only in direct
    modelling designs; parametric designs anchor it to a sketch point in one
    sketch that collects every reference point.
    """
    root = design.rootComponent
    location = adsk.core.Point3D.create(*mm_to_api(world_xyz_mm))
    points = root.constructionPoints
    inp = points.createInput()
    if design.designType == adsk.fusion.DesignTypes.DirectDesignType:
        inp.setByPoint(location)
    else:
        sketch = None
        for sk in root.sketches:
            if sk.name == REFERENCE_SKETCH_NAME:
                sketch = sk
                break
        if sketch is None:
            sketch = root.sketches.add(root.xYConstructionPlane)
            sketch.name = REFERENCE_SKETCH_NAME
        inp.setByPoint(sketch.sketchPoints.add(sketch.modelToSketchSpace(location)))
    cp = points.add(inp)
    try:
        cp.name = name
    except Exception:
        pass
    return cp.name


# ----------------------------------------------------------------- handlers
class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, owner: ActionCommand, fixed_kind: Optional[str]) -> None:
        super().__init__()
        self.owner = owner
        self.fixed_kind = fixed_kind

    def notify(self, args: adsk.core.CommandCreatedEventArgs) -> None:
        # No command inputs: Fusion runs execute immediately, without a dialog.
        handler = _ExecuteHandler(self.owner, self.fixed_kind)
        args.command.execute.add(handler)
        _handlers.append(handler)


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, owner: ActionCommand, fixed_kind: Optional[str]) -> None:
        super().__init__()
        self.owner = owner
        self.fixed_kind = fixed_kind

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        if self.fixed_kind == 'open':
            action = PendingAction('open')
        else:
            action, self.owner.pending = self.owner.pending, None
            if action is None:
                return
        self.owner.perform(action)


class _DeferredHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: ActionCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.perform_deferred()
        except Exception as exc:
            log.error('deferred action failed', exc)


class _ReopenHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: ActionCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        self.owner.reopen_now()
