"""Tiny JSON preference store (a handful of keys, remembered between sessions).

Stored in the user's local app data folder, outside the add-in folder, so
updating or reinstalling the add-in never touches user preferences.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

PREFS_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.environ.get('HOME') or os.getcwd(),
                         'FusionIPWInspector')
PREFS_FILE = os.path.join(PREFS_DIR, 'preferences.json')

DEFAULTS: Dict[str, Any] = {
    'last_stock_folder': '',      # where the user last picked a saved stock file
    'copy_format': 'xyz',         # 'xyz' (tab separated) or 'machine' ('X.. Y.. Z..')
    'advanced_expanded': False,   # remember whether the Advanced group was open
    'show_triad': True,           # draw a small WCS triad for the selected setup
    'diagnostics': False,         # write the diagnostics log
}


class Preferences:
    def __init__(self) -> None:
        self._data: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with open(PREFS_FILE, 'r', encoding='utf-8') as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key in DEFAULTS:
                    if key in stored:
                        self._data[key] = stored[key]
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            os.makedirs(PREFS_DIR, exist_ok=True)
            with open(PREFS_FILE, 'w', encoding='utf-8') as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        if self._data.get(key) != value:
            self._data[key] = value
            self.save()
