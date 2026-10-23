"""The header microphone button: live state + no idle repaint storm.

Two defects are pinned here.

1. The button always painted the same static mic-off glyph, so it could not tell
   the user whether J.A.R.V.I.S. was muted, listening, processing or speaking —
   even though the status rail already knew.

2. Its repaint timer started at construction and never stopped, repainting an
   idle button ~60 times a second (``update()`` every 16 ms) for the life of the
   window.
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

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

from PyQt6.QtCore import QPointF  # noqa: E402
from PyQt6.QtGui import QEnterEvent  # noqa: E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_mic_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


def _pump(qapp, ms: int) -> None:
    deadline = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def test_the_repaint_timer_is_idle_until_hover(qapp):
    btn = ui._MicIconButton(size=20)
    assert btn._timer.isActive() is False, \
        "an idle microphone button must not repaint ~60x/second"


def test_hover_shake_starts_and_stops_the_timer(qapp):
    btn = ui._MicIconButton(size=20)
    btn.show()
    qapp.processEvents()
    enter = QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1))
    btn.enterEvent(enter)
    assert btn._timer.isActive() is True, "the hover shake must animate"
    _pump(qapp, 750)
    assert btn._timer.isActive() is False, \
        "the shake timer must stop itself after the 600 ms gesture"


def test_the_button_reflects_each_state(qapp):
    btn = ui._MicIconButton(size=20)
    assert btn.listening_state() == 'listening'
    for state in ('muted', 'processing', 'speaking', 'listening'):
        btn.set_listening_state(state)
        assert btn.listening_state() == state
        # Painting must work in every state (no exception, non-null pixmap).
        assert btn.grab().isNull() is False
    # An unknown state falls back to listening rather than crashing.
    btn.set_listening_state('nonsense')
    assert btn.listening_state() == 'listening'


def test_the_chat_panel_forwards_status_to_the_mic_button(qapp):
    panel = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    try:
        panel.set_status('MUTED')
        assert panel.mic_btn.listening_state() == 'muted'
        panel.set_status('THINKING')
        assert panel.mic_btn.listening_state() == 'processing'
        panel.set_status('SPEAKING')
        assert panel.mic_btn.listening_state() == 'speaking'
        panel.set_status('LISTENING')
        assert panel.mic_btn.listening_state() == 'listening'
    finally:
        panel.close()
        qapp.processEvents()
