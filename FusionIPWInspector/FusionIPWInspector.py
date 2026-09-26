"""Fusion IPW Inspector add-in entry point.

Adds one button, "IPW Inspector", to Manufacture > Inspect. Clicking it asks
Fusion for the in-process stock of the current setup (through the post
engine, see stock/post_export.py), places it as a pickable temporary mesh and
opens the inspector dialog. Everything else lives in the packages next to
this file:

    commands/     the dialog, the action command and the hidden self test
    core/         Setup WCS transform, point formatting, STL reading (pure Python)
    stock/        acquisition providers, temporary mesh, validation
    ui/           toolbar placement and viewport markers
    diagnostics/  quiet log and self test
    utils/        preferences, clipboard, unit conventions
"""
import traceback

import adsk.core

from .commands.inspector_command import InspectorCommand
from .commands.self_test_command import SelfTestCommand
from .diagnostics import log
from .stock.acquisition import StockSessions
from .ui import toolbar
from .utils.prefs import Preferences

_inspector = None
_self_test = None
_sessions = None
_open_definition_id = None


def run(context):
    global _inspector, _self_test, _sessions, _open_definition_id
    app = adsk.core.Application.get()
    try:
        prefs = Preferences()
        log.set_enabled(bool(prefs.get('diagnostics')))
        _sessions = StockSessions(app)
        _sessions.hook_document_events()
        _self_test = SelfTestCommand(app, _sessions)
        _self_test.register()
        _inspector = InspectorCommand(app, prefs, _sessions, _self_test)
        open_definition = _inspector.register()
        _open_definition_id = open_definition.id
        toolbar.add_button(app.userInterface, open_definition)
        log.info('add-in started (Fusion %s)' % app.version)
    except Exception as exc:
        log.error('add-in failed to start', exc)
        if app and app.userInterface:
            app.userInterface.messageBox('IPW Inspector failed to start:\n%s' % traceback.format_exc(), 'IPW Inspector')


def stop(context):
    global _inspector, _self_test, _sessions
    app = adsk.core.Application.get()
    try:
        if _open_definition_id:
            toolbar.remove_button(app.userInterface, _open_definition_id)
        if _inspector is not None:
            _inspector.unregister()
            _inspector = None
        if _self_test is not None:
            _self_test.unregister()
            _self_test = None
        if _sessions is not None:
            _sessions.remove_all_meshes()
            _sessions.unhook()
            _sessions = None
        log.info('add-in stopped')
    except Exception as exc:
        log.error('add-in failed to stop cleanly', exc)
