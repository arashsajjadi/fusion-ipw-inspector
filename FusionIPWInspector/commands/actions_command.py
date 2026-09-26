"""Document changes requested from the inspector dialog, applied outside of it.

Fusion discards model edits made while another command's dialog is open (they
belong to that command's transaction and vanish when it closes), and some
operations are refused while the Manufacture environment is active. Anything
the inspector wants to keep in the document is therefore handed over here:

* load a saved stock file as the temporary stock component,
* remove the temporary stock component,
* create a reference (construction) point.

Flow: the dialog records a ``PendingAction`` and fires a one-shot custom event.
When Fusion delivers it (after the dialog's event handler has returned) the
handler terminates the dialog, performs the action and reopens the dialog where
the user left off. Stock load/remove run through a small dialog-less command
so each is a single undo step; the reference point is a construction point
anchored to a sketch point (parametric designs) or placed directly (direct
modelling designs).
"""
from __future__ import annotations

import traceback
from typing import Callable, List, Optional

import adsk.core
import adsk.fusion

from ..core.ipw_provider import StockError
from ..utils import log
from ..utils.fusion_units import mm_to_api

ACTION_COMMAND_ID = 'FusionIPWInspector_Action'
ACTION_EVENT_ID = 'FusionIPWInspector_RunAction'
REOPEN_EVENT_ID = 'FusionIPWInspector_Reopen'

_handlers: List[adsk.core.EventHandler] = []


class PendingAction:
    def __init__(self, kind: str, **payload) -> None:
        self.kind = kind
        self.payload = payload

    def __getitem__(self, key):
        return self.payload[key]


class ActionCommand:
    """Runs one PendingAction outside the inspector dialog, then reopens the dialog."""

    def __init__(self, app: adsk.core.Application, reopen: Callable[[], None]) -> None:
        self.app = app
        self.ui = app.userInterface
        self._reopen = reopen
        self._definition: Optional[adsk.core.CommandDefinition] = None
        self._event: Optional[adsk.core.CustomEvent] = None
        self.pending: Optional[PendingAction] = None
        self.last_status: str = ''

    # ------------------------------------------------------------ lifecycle
    def register(self) -> None:
        definitions = self.ui.commandDefinitions
        existing = definitions.itemById(ACTION_COMMAND_ID)
        if existing:
            existing.deleteMe()
        self._definition = definitions.addButtonDefinition(ACTION_COMMAND_ID, 'IPW Inspector stock',
                                                           'Loads or removes the IPW Inspector temporary stock.')
        created = _ActionCreatedHandler(self)
        self._definition.commandCreated.add(created)
        _handlers.append(created)
        try:
            self.app.unregisterCustomEvent(ACTION_EVENT_ID)
        except Exception:
            pass
        self._event = self.app.registerCustomEvent(ACTION_EVENT_ID)
        deferred = _DeferredHandler(self)
        self._event.add(deferred)
        _handlers.append(deferred)
        try:
            self.app.unregisterCustomEvent(REOPEN_EVENT_ID)
        except Exception:
            pass
        self._reopen_event = self.app.registerCustomEvent(REOPEN_EVENT_ID)
        reopen = _ReopenHandler(self)
        self._reopen_event.add(reopen)
        _handlers.append(reopen)

    def unregister(self) -> None:
        for event_id in (ACTION_EVENT_ID, REOPEN_EVENT_ID):
            try:
                self.app.unregisterCustomEvent(event_id)
            except Exception:
                pass
        if self._definition:
            try:
                self._definition.deleteMe()
            except Exception:
                pass
        self._definition = None
        _handlers.clear()

    # --------------------------------------------------------------- running
    def run(self, action: PendingAction) -> None:
        """Called from the dialog: schedule the action for right after this event returns."""
        self.pending = action
        self.last_status = ''
        self.app.fireCustomEvent(ACTION_EVENT_ID, action.kind)

    def perform_deferred(self) -> None:
        """Custom event handler body: runs with no command active."""
        action = self.pending
        if action is None:
            return
        try:
            self.ui.terminateActiveCommand()
        except Exception:
            pass
        if action.kind in ('load_stock', 'remove_stock'):
            # Executed through the dialog-less command so the change is one undo step.
            self._definition.execute()
            return
        self.pending = None
        try:
            if action.kind == 'reference_point':
                name = _create_reference_point(action['design'], action['world_xyz_mm'], action['name'])
                self.last_status = 'Reference point created: %s' % name
                log.info('reference point created: %s' % name)
        except Exception as exc:
            self._report_failure(action, exc)
        self._reopen_safely()

    def perform_in_command(self) -> None:
        """Execute handler body of the dialog-less command (stock load / remove)."""
        action, self.pending = self.pending, None
        if action is None:
            return
        try:
            if action.kind == 'load_stock':
                loaded = action['provider'].load(action['path'], action['setup'], action['frames'])
                self.last_status = 'Loaded %s.' % loaded.file_path.replace('\\', '/').split('/')[-1]
            elif action.kind == 'remove_stock':
                removed = action['provider'].temporary.remove_all()
                action['provider'].current = None
                self.last_status = ('Removed %d temporary stock component(s).' % removed if removed
                                    else 'There was no temporary stock to remove.')
            log.info('action %s done: %s' % (action.kind, self.last_status))
        except StockError as exc:
            self.last_status = str(exc)
            self.ui.messageBox(str(exc), 'IPW Inspector')
        except Exception as exc:
            self._report_failure(action, exc)
        self._reopen_safely()

    def _report_failure(self, action: PendingAction, exc: BaseException) -> None:
        log.error('action %s failed' % action.kind, exc)
        self.last_status = 'The action failed: %s' % exc
        self.ui.messageBox('IPW Inspector could not complete the action.\n\n%s' % traceback.format_exc(),
                           'IPW Inspector')

    def _reopen_safely(self) -> None:
        """Reopen the dialog once Fusion is idle again (never from inside another command)."""
        try:
            self.app.fireCustomEvent(REOPEN_EVENT_ID, '')
        except Exception as exc:
            log.error('scheduling the inspector to reopen failed', exc)

    def reopen_now(self) -> None:
        try:
            self._reopen()
        except Exception as exc:
            log.error('reopening the inspector failed', exc)


REFERENCE_SKETCH_NAME = 'IPW Inspector reference points'


def _create_reference_point(design: adsk.fusion.Design, world_xyz_mm, name: str) -> str:
    """Add a named construction point at a world position (mm).

    ``ConstructionPointInput.setByPoint`` accepts a bare Point3D only in direct
    modelling designs. In a parametric design the point is anchored to a sketch
    point instead: one sketch named "IPW Inspector reference points" collects
    every reference point so the timeline stays tidy.
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
        sketch_point = sketch.sketchPoints.add(sketch.modelToSketchSpace(location))
        inp.setByPoint(sketch_point)
    cp = points.add(inp)
    try:
        cp.name = name
    except Exception:
        pass
    return cp.name


# ----------------------------------------------------------------- handlers
class _ReopenHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: ActionCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        self.owner.reopen_now()


class _DeferredHandler(adsk.core.CustomEventHandler):
    def __init__(self, owner: ActionCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        try:
            self.owner.perform_deferred()
        except Exception as exc:
            log.error('deferred action failed', exc)


class _ActionCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, owner: ActionCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CommandCreatedEventArgs) -> None:
        # No command inputs: Fusion runs execute immediately, without a dialog.
        handler = _ActionExecuteHandler(self.owner)
        args.command.execute.add(handler)
        _handlers.append(handler)


class _ActionExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, owner: ActionCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        self.owner.perform_in_command()
