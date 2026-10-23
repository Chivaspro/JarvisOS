"""How J.A.R.V.I.S. launches its child processes — one explicit place.

Why this module exists
----------------------
``main.py`` used to replace ``subprocess.Popen`` itself with a subclass that
forced ``CREATE_NO_WINDOW`` on every child and DROPPED any ``startupinfo``. That
hid console flashes, but the behaviour was global and invisible: every caller —
including one that legitimately wanted a console, or that passed its own
``startupinfo`` — was silently rewritten, and the patch only existed while
``main.py`` happened to be imported (so ``setup_jarvis.py`` needed its own copy).

The preferred pattern is now explicit and per call::

    import core.process_launch as pl
    subprocess.run(cmd, **pl.hidden_kwargs())      # background helper: no console
    subprocess.run(cmd, **pl.visible_kwargs())     # a window the user asked for

``install_default()`` is the documented app-wide BACKSTOP, installed once at
start-up. It keeps the "a desktop app's children never flash a console"
guarantee for the call sites that have not migrated to explicit flags yet, while
fixing the two real defects of the old patch: it PRESERVES a caller's own
``startupinfo`` (the old patch dropped it, which could mask intent) and it can be
bypassed per call with ``visible=True`` for a deliberately visible launch.

Nothing here changes behaviour on POSIX (there is no console window to hide).
"""
from __future__ import annotations

import subprocess
import sys

_IS_WINDOWS = sys.platform == "win32"

#: CREATE_NO_WINDOW (Windows only). Zero elsewhere so the helpers stay safe.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0

_INSTALLED = False


def hidden_kwargs(**extra) -> dict:
    """Keyword args that launch a child with no console window.

    On Windows this ORs ``CREATE_NO_WINDOW`` into any ``creationflags`` the
    caller already set; on POSIX it adds nothing. A caller's ``startupinfo`` is
    never touched.
    """
    kw = dict(extra)
    if _NO_WINDOW:
        kw["creationflags"] = int(kw.get("creationflags", 0) or 0) | _NO_WINDOW
    return kw


def visible_kwargs(**extra) -> dict:
    """Keyword args for a child the user is meant to see.

    The inverse of :func:`hidden_kwargs`: it does NOT add ``CREATE_NO_WINDOW``,
    so a terminal the user asked for keeps its window. Caller flags are kept.
    """
    return dict(extra)


def run_hidden(cmd, **kw):
    """``subprocess.run`` with no console window (thin, explicit helper)."""
    return subprocess.run(cmd, **hidden_kwargs(**kw))


def popen_hidden(cmd, **kw):
    """``subprocess.Popen`` with no console window (thin, explicit helper)."""
    return subprocess.Popen(cmd, **hidden_kwargs(**kw))


def install_default() -> bool:
    """Install the app-wide no-console default for child processes.

    Returns True when the default is active. Windows-only and idempotent.

    Differences from the old inline patch in ``main.py``:

    * a caller's explicit ``startupinfo`` is PRESERVED, not dropped;
    * a caller can opt out for a deliberately visible launch by passing
      ``visible=True`` (consumed here, never forwarded to Popen);
    * installing twice is a no-op, and the marker attribute makes it detectable.
    """
    global _INSTALLED
    if not _IS_WINDOWS or _INSTALLED:
        return _INSTALLED
    original = subprocess.Popen
    if getattr(original, "_jarvis_no_window_default", False):
        _INSTALLED = True
        return True

    class _DefaultPopen(original):      # type: ignore[misc, valid-type]
        _jarvis_no_window_default = True

        def __init__(self, args, **kw):
            if kw.pop("visible", False):
                super().__init__(args, **kw)
                return
            kw["creationflags"] = (int(kw.get("creationflags", 0) or 0)
                                   | _NO_WINDOW)
            super().__init__(args, **kw)

    subprocess.Popen = _DefaultPopen        # type: ignore[assignment]
    _INSTALLED = True
    return True
