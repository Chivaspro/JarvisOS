"""The chat activity word comes from the REAL activity category.

``main.py`` already sends the honest category for tool work — "CODE · game",
"FIND · tournaments", "SEARCH · tariffs". The typing row used to collapse all
of it into generic WORKING/THINKING, so "coding" and "researching" looked
identical. These tests pin the mapping (category → verb, detail preserved) and
the fallback contract for strings that are not category-prefixed.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QWidget  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_activity_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


class _RecChat:
    """Same contract as the real surface: the indicator carries a label."""

    def __init__(self):
        self.shows = 0
        self.words: list[str] = []

    def show_typing_indicator(self, label=None):
        self.shows += 1
        self.words.append(str(label or ''))

    def hide_typing_indicator(self):
        pass


def _stub():
    st = QWidget()
    st.chat = _RecChat()
    st._doing_now_label = None
    st._typing_settle_timer = None
    st._busy = False
    src = ui.RefinedCommandWindow
    for name in ("set_chat_activity", "clear_chat_activity",
                 "_schedule_indicator_settle", "_cancel_indicator_settle",
                 "_set_busy", "is_busy", "_arm_response_watchdog",
                 "_cancel_response_watchdog"):
        setattr(st, name, src.__dict__[name].__get__(st, QWidget))
    return st


@pytest.mark.parametrize("activity,word", [
    ("CODE · game", "CODING · game"),
    ("CODE", "CODING"),
    ("FIND · tournaments", "RESEARCHING · tournaments"),
    ("SEARCH · tariffs", "SEARCHING · tariffs"),
    ("REVIEW · code_helper", "REVIEWING · code_helper"),
    ("ANALYZE · report.pdf", "ANALYZING · report.pdf"),
    ("GENERATE · logo", "GENERATING · logo"),
    ("VIEW · youtube.com", "VIEWING · youtube.com"),
    ("AUTOMATE · notepad", "AUTOMATING · notepad"),
    ("SMART · plan", "THINKING · plan"),
])
def test_activity_word_maps_real_categories(activity, word):
    assert ui.chat_activity_word(activity) == word


@pytest.mark.parametrize("activity", ["Browsing the web", "Scanning document"])
def test_free_form_activity_keeps_the_previous_contract(activity):
    assert ui.chat_activity_word(activity) == "WORKING"


def test_processing_and_working_read_as_thinking():
    # The original contract: these two exact words were the THINKING state.
    assert ui.chat_activity_word("Processing") == "THINKING"
    assert ui.chat_activity_word("Working") == "THINKING"


def test_thinking_and_reasoning_read_as_thinking():
    assert ui.chat_activity_word("Thinking about the plan") == "THINKING"
    assert ui.chat_activity_word("Reasoning") == "THINKING"


def test_set_chat_activity_uses_the_real_word(qapp):
    st = _stub()
    st.set_chat_activity("CODE · game")
    assert st.chat.words == ["CODING · game"], (
        "the chat must say CODING, not a generic WORKING")


def test_set_chat_activity_fallback_is_unchanged(qapp):
    st = _stub()
    st.set_chat_activity("Scanning document")
    assert st.chat.words == ["WORKING"]
