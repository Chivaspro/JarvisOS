"""Chat operation cards: one live, expandable entry per real task.

The chatlog used to say nothing about the work behind a reply. These tests pin
the replacement against the **real** ``TaskRegistry``: its events are fed to the
same handler the app uses, and the resulting card is checked for:

* creation on real work only (a pure answer posts nothing),
* update-in-place for every later step event (never a second card),
* real step titles/outcomes in the expandable details,
* final COMPLETE / FAILED / CANCELLED states that later events cannot reopen.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("PyQt6")

from .qt_harness import qapp  # noqa: F401  (fixture)

# ``tests/ui`` is a package named ``ui``, so load the app module under an alias.
_ui_spec = importlib.util.spec_from_file_location("ui_impl_operation_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)

from brain.task_engine import TaskRegistry  # noqa: E402


class _Host:
    """The pieces of RefinedCommandWindow the card handler really uses."""

    def __init__(self):
        self.chat = ui._RoundedChatLog()
        self.chat.resize(560, 620)
        self.chat.show()
        self._task_cards = {}

    def apply_task_event_card(self, payload):
        return ui.RefinedCommandWindow.apply_task_event_card(self, payload)


def _host():
    host = _Host()
    host.apply_task_event_card = types.MethodType(
        ui.RefinedCommandWindow.apply_task_event_card, host)
    host._live_operation_card = types.MethodType(
        ui.RefinedCommandWindow._live_operation_card, host)
    return host


@pytest.fixture()
def registry(tmp_path):
    reg = TaskRegistry(db_path=tmp_path / "tasks.db",
                       reports_dir=tmp_path / "reports",
                       emit_logs=False)
    yield reg


def _rows(host):
    return list(getattr(host.chat, "_rows", []))


def test_a_real_task_posts_one_card_and_updates_it_in_place(
        qapp, registry, tmp_path):
    host = _host()
    registry.add_listener(host.apply_task_event_card)
    try:
        tid = registry.begin_request("Find the latest Python release",
                                     source="text", category="SEARCH")
        qapp.processEvents()
        assert _rows(host) == [], "task_created alone must not post a card"

        step = registry.note_tool_started(
            tid, "web_search", detail="Python latest release", corr="c1")
        qapp.processEvents()
        rows = _rows(host)
        assert len(rows) == 1, "the first real step must open exactly one card"
        card = rows[0]
        assert isinstance(card, ui._ChatOperationCard)
        assert card.title_text() == "Find the latest Python release"
        assert "RUNNING" in card.state_text()
        assert any("Python latest release" in line for line in card.step_lines())

        registry.note_tool_result(
            tid, "c1", "SUCCESS", "Python 3.14 is the current release",
            step_id=step)
        registry.mark_validating(tid)
        qapp.processEvents()
        assert len(_rows(host)) == 1, "a step event must never add a second card"
        assert "VERIFYING" in card.state_text()

        registry.complete(tid, summary="Found the current Python release")
        qapp.processEvents()
        assert len(_rows(host)) == 1
        assert card.is_finished
        assert "COMPLETE" in card.state_text()
        assert "current Python release" in card.stage_text()

        # Late events for a finished task cannot reopen or duplicate it.
        registry.note_finding(tid, "late finding")
        qapp.processEvents()
        assert len(_rows(host)) == 1
        assert card.is_finished
    finally:
        host.chat.deleteLater()


def test_a_failed_task_ends_the_card_in_the_failed_state(qapp, registry):
    host = _host()
    registry.add_listener(host.apply_task_event_card)
    try:
        tid = registry.begin_request("Open the editor", category="AUTOMATE")
        registry.note_tool_started(tid, "pc_control", detail="open editor",
                                   corr="c2")
        registry.note_tool_result(tid, "c2", "FAILED", "the window was not found",
                                  step_id="")
        registry.fail(tid, "the window was not found")
        qapp.processEvents()

        rows = _rows(host)
        assert len(rows) == 1
        assert rows[0].is_finished
        assert "FAILED" in rows[0].state_text()
        assert "not found" in rows[0].stage_text()
    finally:
        host.chat.deleteLater()


def test_a_cancelled_task_ends_the_card_in_the_cancelled_state(qapp, registry):
    host = _host()
    registry.add_listener(host.apply_task_event_card)
    try:
        tid = registry.begin_request("Research this for me", category="SEARCH")
        registry.note_tool_started(tid, "web_search", detail="research", corr="c3")
        registry.cancel(tid, "cancelled by the user")
        qapp.processEvents()

        rows = _rows(host)
        assert len(rows) == 1
        assert rows[0].is_finished
        assert "CANCELLED" in rows[0].state_text()
    finally:
        host.chat.deleteLater()


def test_a_pure_answer_never_posts_an_operation_card(qapp, registry):
    host = _host()
    registry.add_listener(host.apply_task_event_card)
    try:
        tid = registry.begin_request("What is a monad?", source="voice",
                                     category="CHAT")
        registry.note_analysis(tid, detail="clear request")
        registry.complete(tid, summary="answered")
        qapp.processEvents()
        assert _rows(host) == [], "bookkeeping-only tasks are not operations"
    finally:
        host.chat.deleteLater()


def test_expanding_shows_the_real_steps_and_outcomes(qapp, registry):
    host = _host()
    registry.add_listener(host.apply_task_event_card)
    try:
        tid = registry.begin_request("Write the parser", category="CODE")
        step = registry.note_tool_started(tid, "code_helper",
                                          detail="parser.py", corr="c4")
        registry.note_tool_result(tid, "c4", "SUCCESS",
                                  "parser.py written (42 lines)", step_id=step)
        registry.complete(tid, summary="Parser written")
        qapp.processEvents()

        card = _rows(host)[0]
        assert not card.details_visible()
        card.toggle_details()
        qapp.processEvents()
        assert card.details_visible()

        body = card.details_text()
        assert "code_helper" in body
        assert "parser.py written (42 lines)" in body
    finally:
        host.chat.deleteLater()
