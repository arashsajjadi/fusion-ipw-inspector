"""Quiet-by-default diagnostics log.

Nothing is written unless diagnostics are enabled (Advanced > Diagnostics log,
or the preference ``diagnostics``). The log lives in the user's local app data
folder so it never ends up inside the add-in or a design file.
"""
from __future__ import annotations

import datetime
import os
import traceback

LOG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.environ.get('HOME') or os.getcwd(),
                       'FusionIPWInspector')
LOG_FILE = os.path.join(LOG_DIR, 'diagnostics.log')

_enabled = False


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)
    if _enabled:
        info('diagnostics enabled')


def is_enabled() -> bool:
    return _enabled


def log_path() -> str:
    return LOG_FILE


def info(message: str) -> None:
    if _enabled:
        _write('INFO', message)


def error(message: str, exc: BaseException | None = None) -> None:
    """Errors are always recorded; they are rare and useful for bug reports."""
    text = message
    if exc is not None:
        text += '\n' + ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _write('ERROR', text)


def _write(level: str, message: str) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write('%s %-5s %s\n' % (stamp, level, message))
    except OSError:
        pass
