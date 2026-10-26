"""Closing every surface must still reach the one shutdown coordinator.

Why this file exists: ``tests/unit/test_full_closure.py`` pinned the literal
``request_process_exit('close-all-ui')`` in ``ui.py``. The HUD then grew a
deliberate fade-out outro (``_close_all_ui`` → ``_ShutdownFadeOut`` →
``_finish_close_all_ui`` → ``self.close()`` → ``closeEvent`` → coordinator), so
that literal disappeared while the BEHAVIOUR it protected - every close path
converging on the coordinator instead of a bare ``QApplication.quit()`` - did
not. A string guard cannot tell those two situations apart; this test drives
the real path and records the real call.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402  (the session's QApplication fixture)

from core import stability  # noqa: E402

# ui.py is loaded BY PATH, exactly like the other HUD tests: inside this package
# a plain ``import ui`` can resolve to tests/ui/ itself.
_ui_spec = importlib.util.spec_from_file_location("ui_impl_shutdown_funnel", ROOT / "ui.py")
ui_mod = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui_mod
_ui_spec.loader.exec_module(ui_mod)


@pytest.fixture()
def window(qapp, tmp_path):
    win = ui_mod.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()
    yield win
    try:
        win.close()
    except Exception:
        pass


def test_closing_every_surface_reaches_the_shutdown_coordinator(qapp, window,
                                                               monkeypatch):
    """The regression this protects: a close path that bypasses the coordinator
    leaves the mic, MCP servers and browser workers orphaned."""
    calls: list = []
    monkeypatch.setattr(stability, "request_process_exit",
                        lambda *args, **kwargs: calls.append(args), raising=False)

    window._close_all_ui()          # the fade-out outro

    deadline = time.monotonic() + 8.0
    while not calls and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)

    assert calls, ("closing every surface must converge on "
                   "core.stability.request_process_exit")
    asserted_reason = str(calls[0][0]) if calls[0] else ""
    assert asserted_reason, "the coordinator must be told WHY the process is leaving"


def test_the_final_teardown_ends_in_the_window_close_path(qapp, window,
                                                          monkeypatch):
    """Even with the outro skipped (HUD already hidden), the same funnel runs."""
    calls: list = []
    monkeypatch.setattr(stability, "request_process_exit",
                        lambda *args, **kwargs: calls.append(args), raising=False)

    window._finish_close_all_ui()
    qapp.processEvents()

    assert calls, "the fade-less path must reach the coordinator too"
