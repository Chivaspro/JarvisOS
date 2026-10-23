"""The chat-panel crash fallback must construct a DIFFERENT class.

``_CommandWindow`` is aliased to ``RefinedCommandWindow``; the fallback used
that alias, so when the refined construction raised, the retry raised
identically and the user was left with no CHAT panel at all — while the
activity log promised a "recovered" host that never existed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

# A real MainWindow with an isolated config and a mocked camera, as in
# test_panel_drag.py. The `ui` fixture is the app's ui.py loaded by the harness
# under an alias — `import ui` here would resolve to the tests/ui package.
from .test_camera_pipeline import _pump, camera, qapp, ui  # noqa: F401,E402
from .test_image_view_panel import window  # noqa: F401,E402


def test_the_fallback_target_is_the_real_base_class(qapp):
    assert ui._CommandWindowBase is not ui.RefinedCommandWindow, (
        "_CommandWindowBase must be the genuine base, not the refined alias")
    assert issubclass(ui.RefinedCommandWindow, ui._CommandWindowBase)


def _break_refined(monkeypatch):
    def _boom(self, host):
        raise RuntimeError("simulated refined-panel failure")

    monkeypatch.setattr(ui.RefinedCommandWindow, "__init__", _boom)


def test_a_failing_refined_build_still_produces_a_chat_panel(window, qapp, monkeypatch):
    """Break RefinedCommandWindow.__init__ and toggle the chat panel: the
    fallback must construct the BASE window (a different class) and the window
    must actually open."""
    import traceback as _tb
    captured = []
    monkeypatch.setattr(_tb, "print_exc",
                        lambda *a, **k: captured.append(_tb.format_exc()))
    logs = []
    monkeypatch.setattr(window, "write_log", lambda text: logs.append(str(text)))
    monkeypatch.setattr(window, "run_on_ui", lambda fn: fn())

    _break_refined(monkeypatch)
    monkeypatch.setattr(window, "_command_window", None)

    window._toggle_command_window()
    _pump(qapp, 0.05)

    win = getattr(window, "_command_window", None)
    assert win is not None, (
        "the fallback produced no chat panel; logs:\n" + "\n".join(logs)
        + "\nswallowed tracebacks:\n" + "\n---\n".join(captured))
    assert type(win) is ui._CommandWindowBase, (
        "the fallback must be the base class, not the refined one again")
    assert win.isVisible()


def test_the_base_fallback_seeds_no_fake_conversation(window, qapp, monkeypatch):
    _break_refined(monkeypatch)
    monkeypatch.setattr(window, "_command_window", None)

    window._toggle_command_window()
    _pump(qapp, 0.05)

    win = getattr(window, "_command_window", None)
    assert win is not None
    text = win.chat.toPlainText()
    assert "What's the status of my system?" not in text
    assert "Everything appears to be in order" not in text
    # And the metrics line must not invent numbers.
    assert "CPU 24%" not in win.chips.text()
