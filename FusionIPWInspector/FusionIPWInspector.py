"""Fusion IPW Inspector add-in entry point.

Adds one button, "IPW Inspector", to Manufacture > Inspect. Everything else
lives in the packages next to this file:

    commands/   the dialog and its event handlers
    core/       stock acquisition, Setup WCS transform, point formatting
    ui/         toolbar placement and viewport markers
    utils/      logging, preferences, clipboard, unit conventions
"""
import traceback

import adsk.core

from .commands.inspector_command import InspectorCommand
from .utils import log
from .utils.prefs import Preferences

_command = None


def run(context):
    global _command
    app = adsk.core.Application.get()
    try:
        prefs = Preferences()
        log.set_enabled(bool(prefs.get('diagnostics')))
        _command = InspectorCommand(app, prefs)
        _command.register()
        log.info('add-in started (Fusion %s)' % app.version)
    except Exception as exc:
        log.error('add-in failed to start', exc)
        if app and app.userInterface:
            app.userInterface.messageBox('IPW Inspector failed to start:\n%s' % traceback.format_exc(),
                                         'IPW Inspector')


def stop(context):
    global _command
    try:
        if _command is not None:
            _command.unregister()
            _command = None
        log.info('add-in stopped')
    except Exception as exc:
        log.error('add-in failed to stop cleanly', exc)
