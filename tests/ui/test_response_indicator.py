"""The Chat Log says what J.A.R.V.I.S. is doing, and says when it has stopped.

The typing row used to be animated dots only: a stalled request looked exactly
like a slow one, and the panel could sit "working" forever. These tests pin the
two halves of the contract:

* the typing row carries a real word (``WORKING…`` / ``THINKING…``), not just
  dots;
* an activity burst arms ONE response watchdog. If nothing at all arrives it
  ends the busy state and writes a plain notice into the log — it never
  invents an assistant reply and never records one in the conversation store;
* a real reply, or the real end of the turn, cancels that watchdog. A tool
  finishing does not: more of the same turn may still be on its way.
"""
from __future__ import annotations

import pytest

from PyQt6.QtTest import QTest

from .test_camera_pipeline import qapp, ui  # noqa: F401  (fixtures)


class _Host:
    """The minimum host the panel talks to; records instead of acting."""

    def __init__(self):
        self.logs: list[str] = []
        self.commands: list[str] = []

    def write_log(self, text):
        self.logs.append(str(text))

    def _activity_add(self, text):
        pass

    def _send_backend_command(self, text, log=True):
        self.commands.append(str(text))

    def run_on_ui(self, fn):
        fn()


@pytest.fixture()
def panel(qapp):
    win = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    win._host = _Host()
    # The timeout notice is UI-side; it must not touch a real transcript.
    recorded: list[tuple[str, str]] = []
    win.record_message = lambda role, text: recorded.append((str(role), str(text))) or ''
    win._test_recorded = recorded
    yield win
    try:
        win.close()
    except Exception:
        pass
    qapp.processEvents()


def _wait(qapp, ms: int) -> None:
    QTest.qWait(ms)
    qapp.processEvents()


def _notices(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatNoticeRow)]


def test_the_typing_row_carries_a_real_word_not_only_dots(qapp):
    log = ui._RoundedChatLog()
    log.show_typing_indicator('WORKING')
    row = log._typing_row
    assert row is not None
    assert row.activity_text() == 'WORKING…', (
        "the indicator must say what is happening, not only animate dots")

    log.show_typing_indicator('THINKING')
    assert row.activity_text() == 'THINKING…'

    # The bubble has to make room for the word instead of clipping it.
    assert row._bubble.width() >= row._label.width(), (
        "the activity word is wider than its bubble")


def test_activity_shows_thinking_or_working_and_arms_one_watchdog(qapp, panel):
    panel.set_chat_activity('Thinking')
    assert panel.chat._typing_row.activity_text() == 'THINKING…'
    assert panel.is_busy()
    assert panel.response_watchdog_active()

    panel.set_chat_activity('Browsing the web')
    assert panel.chat._typing_row.activity_text() == 'WORKING…', (
        "anything that is not thinking is generic work")
    assert panel.is_busy()


def test_a_silent_request_times_out_with_a_clear_log_notice(qapp, panel, monkeypatch):
    monkeypatch.setenv('JARVIS_RESPONSE_TIMEOUT_SECONDS', '0.12')
    panel.set_chat_activity('Thinking')
    assert panel.response_watchdog_active()

    _wait(qapp, 400)

    assert not panel.is_busy(), "a timed-out request must release the Stop control"
    assert not panel.chat.has_typing_indicator(), "the dots would still promise a reply"
    assert not panel.response_watchdog_active()
    notices = _notices(panel)
    assert len(notices) == 1, f"expected exactly one timeout notice, got {notices!r}"
    text = notices[0].text()
    assert 'NO RESPONSE' in text
    assert '0.12' in text, "the notice must state the timeout it actually used"


def test_a_timeout_never_invents_an_assistant_reply(qapp, panel, monkeypatch):
    monkeypatch.setenv('JARVIS_RESPONSE_TIMEOUT_SECONDS', '0.1')
    panel.set_chat_activity('Thinking')
    _wait(qapp, 350)

    assert panel._test_recorded == [], (
        "a UI timeout is not an assistant turn and must never be recorded as one")
    assert panel._chat_entries == []
    assert not any(isinstance(r, ui._ChatMessageRow) for r in panel.chat._rows)


def test_a_real_reply_cancels_the_timeout(qapp, panel, monkeypatch):
    monkeypatch.setenv('JARVIS_RESPONSE_TIMEOUT_SECONDS', '0.12')
    panel.set_chat_activity('Thinking')

    panel.append_msg('J.A.R.V.I.S.', 'Done, sir.')

    assert not panel.response_watchdog_active()
    assert not panel.is_busy()
    assert not panel.chat.has_typing_indicator()
    _wait(qapp, 350)
    assert _notices(panel) == [], "the reply landed; no timeout notice is allowed"


def test_a_finished_tool_does_not_end_the_turn_but_turn_finished_does(qapp, panel):
    panel.set_chat_activity('Working')

    # This is what main.py calls after every tool response.
    panel.clear_chat_activity()
    assert panel.is_busy(), "a finished tool is not a finished turn"
    assert panel.response_watchdog_active(), (
        "the rest of the turn is still awaited after a tool returns")

    panel.turn_finished()

    assert not panel.is_busy()
    assert not panel.chat.has_typing_indicator()
    assert not panel.response_watchdog_active()
    assert _notices(panel) == [], "a normal turn end is not a timeout"
