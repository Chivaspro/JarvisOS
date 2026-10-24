"""Single source of truth for "is JARVIS actually ready?".

Before this module three different answers existed at once:

* the launcher splash called the app ONLINE the moment ``main.py`` was
  spawned (``start_jarvis.py``),
* the setup overlay called it LISTENING the moment a key was saved
  (``ui.py``), and
* the HUD stayed on INITIALISING until the Gemini Live session connected.

They disagreed on every slow or failed boot. This module owns the transition
instead, so each surface renders the same state and only a real connection can
clear the boot indicator.

Design rules:

* Standard library only and Qt-free: ``main.py`` writes, ``ui.py`` subscribes.
* Transitions are monotonic where it matters. Once ``READY`` has been reached a
  late ``BOOTING`` / ``CLEANUP`` / ``STARTING_UI`` is ignored, so a stale caller
  can never bring "System initializing" back. A later ``CONNECTING`` *is*
  accepted - that is a reconnect, and the snapshot's ``ever_ready`` flag lets
  the UI say RECONNECTING rather than INITIALISING.
* Logging and listener callbacks never raise. A broken log path or a dead
  widget must not change the state.

The watch dog exists because the old connect loop retried forever with no
visible message. It promotes a stalled ``CONNECTING`` to ``DEGRADED`` with an
actionable detail; the backend keeps retrying in the background.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

__all__ = [
    "BOOTING", "CLEANUP", "STARTING_UI", "CONNECTING", "READY",
    "DEGRADED", "ERROR", "STATES",
    "configure", "current", "snapshot", "set_state", "subscribe", "event",
    "arm_watchdog", "reset_for_tests",
]

BOOTING = "BOOTING"
CLEANUP = "CLEANUP"
STARTING_UI = "STARTING_UI"
CONNECTING = "CONNECTING"
READY = "READY"
DEGRADED = "DEGRADED"
ERROR = "ERROR"

STATES = (BOOTING, CLEANUP, STARTING_UI, CONNECTING, READY, DEGRADED, ERROR)
_BOOT_ONLY = frozenset((BOOTING, CLEANUP, STARTING_UI))

_lock = threading.RLock()
_log_lock = threading.Lock()

_state: str = BOOTING
_detail: str = ""
_ever_ready: bool = False
_started: float = time.monotonic()
_since: float = _started
_listeners: list[Callable[[str, str], None]] = []
_history: list[tuple[str, str, float]] = []
_log_path: Path | None = None

_watchdog: threading.Thread | None = None
_watchdog_disarmed: bool = False


def _watchdog_enabled() -> bool:
    return os.environ.get("JARVIS_STARTUP_WATCHDOG", "1").strip().lower() not in (
        "0", "false", "no", "off")


def configure(log_path: str | os.PathLike[str] | None) -> None:
    """Point the audit log at ``log_path``; ``None`` disables file logging."""
    global _log_path
    if log_path is None:
        _log_path = None
        return
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _log_path = path
    except Exception:
        _log_path = None


def _write_line(kind: str, tag: str, detail: str = "") -> None:
    path = _log_path
    if path is None:
        return
    now = time.time()
    elapsed = max(0.0, time.monotonic() - _started)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    millis = int((now % 1.0) * 1000)
    line = f"{stamp}.{millis:03d}  +{elapsed:8.3f}s  {kind:<5}  {tag}"
    if detail:
        line += f"  |  {detail}"
    try:
        with _log_lock:
            with open(path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(line + "\n")
    except Exception:
        pass


def current() -> tuple[str, str]:
    """Return ``(state, detail)``."""
    with _lock:
        return _state, _detail


def snapshot() -> dict:
    """Everything a UI or a report needs, in one atomic read."""
    with _lock:
        now = time.monotonic()
        return {
            "state": _state,
            "detail": _detail,
            "ever_ready": _ever_ready,
            "elapsed": round(max(0.0, now - _started), 3),
            "since_seconds": round(max(0.0, now - _since), 3),
            "history": list(_history),
        }


def set_state(state: str, detail: str = "") -> str:
    """Move to ``state``. Unknown states and stale boot states are ignored.

    Returns the state that is actually current afterwards.
    """
    global _state, _detail, _since, _ever_ready, _watchdog_disarmed
    try:
        wanted = str(state or "").strip().upper()
    except Exception:
        return _state
    if wanted not in STATES:
        return _state

    with _lock:
        if _ever_ready and wanted in _BOOT_ONLY:
            return _state
        if wanted == _state and detail == _detail:
            return _state

        _state = wanted
        _detail = str(detail or "")
        _since = time.monotonic()
        if wanted == READY:
            _ever_ready = True
            _watchdog_disarmed = True
        elif wanted == ERROR:
            _watchdog_disarmed = True
        _history.append((wanted, _detail, round(_since - _started, 3)))
        del _history[:-200]
        listeners = list(_listeners)

    _write_line("STATE", wanted, detail)
    for callback in listeners:
        try:
            callback(wanted, str(detail or ""))
        except Exception:
            pass
    return wanted


def subscribe(callback: Callable[[str, str], None]):
    """Register ``callback(state, detail)``; returns an unsubscribe function."""
    if not callable(callback):
        return lambda: None
    with _lock:
        _listeners.append(callback)

    def _unsubscribe() -> None:
        with _lock:
            try:
                _listeners.remove(callback)
            except ValueError:
                pass

    return _unsubscribe


def event(tag: str, detail: str = "") -> None:
    """Record a one-off startup milestone (the existing ``[STARTUP]`` markers)."""
    try:
        _write_line("EVENT", str(tag or "?"), detail)
    except Exception:
        pass


def arm_watchdog(seconds: float = 30.0,
                 detail: str = ("Still connecting to Gemini - check the API "
                                "key and the network. Retrying in the "
                                "background.")) -> None:
    """Promote a stalled CONNECTING to DEGRADED after ``seconds``.

    Never overrides READY or ERROR, and never stops the retry loop.
    """
    global _watchdog, _watchdog_disarmed
    if not _watchdog_enabled():
        return

    with _lock:
        if _state in (READY, ERROR) or _ever_ready:
            return
        if _watchdog is not None and _watchdog.is_alive():
            return
        _watchdog_disarmed = False
        timeout = max(0.01, float(seconds))
        message = str(detail or "")

        def _watch() -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if _watchdog_disarmed:
                    return
                if _state in (READY, ERROR):
                    return
                time.sleep(0.02)
            with _lock:
                if _watchdog_disarmed or _ever_ready or _state in (READY, ERROR):
                    return
            set_state(DEGRADED, message)

        _watchdog = threading.Thread(
            target=_watch, name="startup-watchdog", daemon=True)
        _watchdog.start()


def reset_for_tests() -> None:
    """Restore the module to a fresh boot. Test-only."""
    global _state, _detail, _ever_ready, _started, _since
    global _listeners, _history, _log_path, _watchdog, _watchdog_disarmed
    with _lock:
        _state = BOOTING
        _detail = ""
        _ever_ready = False
        _started = _since = time.monotonic()
        _listeners = []
        _history = []
        _log_path = None
        _watchdog = None
        _watchdog_disarmed = False
