"""Put text on the system clipboard without third-party packages.

Windows uses the Win32 clipboard through ctypes. macOS uses ``pbcopy``.
Returns True on success so the UI can tell the user when copying failed.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys


def copy_text(text: str) -> bool:
    try:
        if sys.platform.startswith('win'):
            return _copy_windows(text)
        if sys.platform == 'darwin':
            proc = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
            proc.communicate(text.encode('utf-8'))
            return proc.returncode == 0
    except Exception:
        return False
    return False


def _copy_windows(text: str) -> bool:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    data = text.encode('utf-16-le') + b'\x00\x00'
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()
