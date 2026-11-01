"""The live coding card: real stages, streamed code, real diffs.

When the user asks "build me a game", the chat must show that J.A.R.V.I.S. is
actually coding — not a silent chat. These tests pin the card lifecycle on the
real window (offscreen):

* a ``start`` event opens exactly ONE live card and lights the CODING indicator;
* ``file``/``diff``/``stage`` events update it and re-arm the response watchdog
  (the build is long; the watchdog must not falsely declare a timeout);
* ``done`` finalizes the card and clears the live reference;
* a reply landing in the chat finalizes any still-live card (safety net).
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

from PyQt6.QtTest import QTest  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_coding_card_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


class _Host:
    """The minimum host the panel talks to; records instead of acting."""

    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(str(text))

    def _activity_add(self, text):
        pass

    def _send_backend_command(self, text, log=True):
        pass

    def run_on_ui(self, fn):
        fn()


@pytest.fixture()
def panel(qapp):
    win = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    win._host = _Host()
    win.record_message = lambda role, text: ''
    yield win
    try:
        win.close()
    except Exception:
        pass
    qapp.processEvents()


def _cards(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatCodingCard)]


def _notices(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatNoticeRow)]


def test_start_opens_one_live_card_and_lights_coding(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'pong game',
                                         'language': 'python',
                                         'project': 'pong'})
    cards = _cards(panel)
    assert len(cards) == 1, "a coding session must open exactly one live card"
    assert panel._coding_card is cards[0]
    row = panel.chat._typing_row
    assert row is not None, "the working indicator must be visible while coding"
    assert 'CODING' in row.activity_text()
    assert panel.response_watchdog_active() is True
    assert panel.is_busy() is True


def test_two_starts_reuse_one_card(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'pong'})
    panel.apply_coding_preview('start', {'description': 'pong'})
    assert len(_cards(panel)) == 1


def test_file_event_streams_code_into_the_card(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'pong', 'project': 'pong'})
    panel.apply_coding_preview('file', {'path': 'src/main.py',
                                        'code': 'print("hi")\n',
                                        'index': 1, 'total': 3,
                                        'language': 'python'})
    card = panel._coding_card
    assert card is not None
    card.flush_stream()
    assert 'print("hi")' in card.code_text()
    assert 'main.py' in card.file_label_text()


def test_diff_event_shows_the_changes(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'fix it', 'project': 'proj'})
    panel.apply_coding_preview('diff', {'path': 'main.py',
                                        'diff': '--- main.py\n+++ main.py\n-broken\n+fixed\n'})
    card = panel._coding_card
    assert card is not None
    text = card.code_text()
    assert '-broken' in text
    assert '+fixed' in text


def test_done_finalizes_the_card(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'pong', 'project': 'pong'})
    panel.apply_coding_preview('done', {'summary': 'Built pong — runs and exits cleanly.',
                                        'success': True,
                                        'files': ['main.py', 'utils.py']})
    cards = _cards(panel)
    assert len(cards) == 1
    card = cards[0]
    assert card.is_finished is True
    assert 'runs and exits cleanly' in card.stage_text()
    assert panel._coding_card is None, "a finished card is no longer live"


def test_failed_event_marks_the_card_finished(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'pong', 'project': 'pong'})
    panel.apply_coding_preview('failed', {'summary': 'Rate limit reached.'})
    assert panel._coding_card is None
    card = _cards(panel)[0]
    assert card.is_finished is True
    assert 'Rate limit' in card.stage_text()


def test_progress_keeps_the_watchdog_alive(qapp, panel, monkeypatch):
    """A long build must not be declared "no response" while it is working."""
    monkeypatch.setenv('JARVIS_RESPONSE_TIMEOUT_SECONDS', '0.5')
    panel.apply_coding_preview('start', {'description': 'pong', 'project': 'pong'})
    QTest.qWait(300)
    qapp.processEvents()
    assert panel.response_watchdog_active()

    panel.apply_coding_preview('stage', {'stage': 'Writing main.py...'})
    QTest.qWait(300)
    qapp.processEvents()
    assert panel.is_busy(), "coding progress must keep the working state alive"
    assert panel.response_watchdog_active(), "progress did not re-arm the watchdog"
    assert _notices(panel) == [], "no timeout notice while real progress arrives"


def test_a_reply_landing_finalizes_a_live_card(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'pong', 'project': 'pong'})
    panel.append_msg('J.A.R.V.I.S.', 'Built and run, sir.')
    assert panel._coding_card is None
    assert _cards(panel)[0].is_finished is True


def test_unknown_kinds_are_ignored(qapp, panel):
    panel.apply_coding_preview('start', {'description': 'pong'})
    panel.apply_coding_preview('what-is-this', {'x': 1})
    assert panel._coding_card is not None


def test_clearing_the_chat_mid_build_reopens_a_card_on_the_next_write(qapp, panel):
    """Clearing the chat deletes the card widget; the build keeps reporting."""
    panel.apply_coding_preview('start', {'description': 'pong', 'project': 'pong'})
    panel.chat.clear()
    qapp.processEvents()
    panel.apply_coding_preview('file', {'path': 'main.py', 'code': 'x = 1',
                                        'index': 1, 'total': 2})
    panel.apply_coding_preview('stage', {'stage': 'Writing util.py…'})
    cards = _cards(panel)
    assert len(cards) == 1, "a cleared card must not swallow the rest of the build"
    assert panel._coding_card is cards[0]
    assert 'util.py' in cards[0].stage_text()
