"""Voice turns become real, recorded chat messages — even with the panel hidden.

The bug this pins: a spoken command was displayed in the command box and then
executed by the Live session, but it only reached the Chat Log (and the
conversation store) through a log-string path gated on the panel being VISIBLE.
Tucked away — or never opened — the turn vanished from chat and history.

The fix is a single submission path (``ui.MainWindow.submit_chat_turn`` ->
``_apply_chat_turn``) that always displays and records a finished turn, plus
existence-based gates on the reply/stream routes. These tests drive the real
implementations (bound to a lightweight host, as other UI tests do) and assert
the store, not a mock.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_voice_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


class _SignalStub:
    """Synchronous stand-in for a queued pyqtSignal (deterministic in tests)."""

    def __init__(self, handler):
        self._handler = handler

    def emit(self, *args):
        return self._handler(*args)


class _Host:
    def __init__(self, panel=None):
        self.panel = panel

    def run_on_ui(self, fn):
        return fn()

    def write_log(self, text):
        return None

    def clear_chat_activity(self):
        return None


class _ActivityLog:
    def __init__(self):
        self.entries = []

    def add_entry(self, text):
        self.entries.append(str(text))


class _MainStub:
    """A MainWindow-shaped host wired to the REAL submission methods."""

    def __init__(self, win=None):
        self._command_window = win
        self._assistant_name = 'JARVIS'
        self._chat_history = []
        self._activity = _ActivityLog()
        self.status = []

    # Minimal surface the bound MainWindow methods touch.
    def run_on_ui(self, fn):
        return fn()


def _bind(stub):
    for name in ('_live_command_window', '_apply_chat_turn',
                 '_activity_add', '_append_chat',
                 'begin_assistant_stream', '_stream_assistant_text_ui',
                 'finish_assistant_stream', '_log_from_backend'):
        if hasattr(ui.MainWindow, name):
            setattr(stub, name, types.MethodType(getattr(ui.MainWindow, name), stub))
    # `submit_chat_turn` emits a pyqtSignal on the real window; a synchronous
    # stub keeps the test deterministic while exercising the same handler.
    setattr(stub, 'submit_chat_turn',
            types.MethodType(ui.MainWindow.submit_chat_turn, stub))
    stub._chat_turn_sig = _SignalStub(stub._apply_chat_turn)
    # `_log_from_backend` also pokes a status label; keep it harmless.
    stub._status = types.SimpleNamespace(setText=lambda *_a: None)
    return stub


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from memory import conversations

    instance = conversations.ConversationStore(tmp_path / "conversations.db")
    monkeypatch.setattr(conversations, "get_store", lambda *a, **k: instance)
    yield instance
    instance.close()


@pytest.fixture()
def panel(qapp, store):
    win = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    win._host = _Host(win)
    yield win
    try:
        win.close()
    except Exception:
        pass
    qapp.processEvents()


def _rows(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatMessageRow)]


def _texts(panel):
    out = []
    for row in _rows(panel):
        try:
            out.append((row._bubble._raw_text, bool(getattr(row, '_muted', False))))
        except Exception:
            out.append(('', False))
    return out


# ── the submission path ──────────────────────────────────────────────────────

def test_voice_utterance_is_shown_and_recorded_with_the_panel_visible(qapp, panel, store):
    stub = _bind(_MainStub(panel))
    stub.submit_chat_turn('user', 'Jarvis, open YouTube')
    qapp.processEvents()

    texts = [t for t, _ in _texts(panel)]
    assert 'Jarvis, open YouTube' in texts, "the utterance is not a chat bubble"
    conv_id = panel.active_conversation_id()
    assert conv_id
    rows = store.messages(conv_id)
    assert [(r['role'], r['text']) for r in rows] == [('user', 'Jarvis, open YouTube')]


def test_voice_utterance_is_shown_and_recorded_while_hidden(qapp, panel, store):
    """The reported bug: a tucked-away panel must still get the turn."""
    stub = _bind(_MainStub(panel))
    panel.hide()
    qapp.processEvents()
    assert panel.isVisible() is False

    stub.submit_chat_turn('user', 'Jarvis, open YouTube')
    qapp.processEvents()

    assert 'Jarvis, open YouTube' in [t for t, _ in _texts(panel)], \
        "a hidden panel dropped the spoken command from the chat"
    rows = store.messages(panel.active_conversation_id())
    assert [(r['role'], r['text']) for r in rows] == [('user', 'Jarvis, open YouTube')], \
        "a hidden panel failed to record the spoken command"


def test_reply_is_shown_and_recorded_while_hidden(qapp, panel, store):
    """The reply route had the same visibility gate as the user turn."""
    stub = _bind(_MainStub(panel))
    # Give the panel an active conversation first (a prior turn).
    panel.append_msg('You', 'hello', you=True)
    panel.hide()
    qapp.processEvents()

    stub._log_from_backend('J.A.R.V.I.S.: At your service, sir.')
    qapp.processEvents()

    assert 'At your service, sir.' in [t for t, _ in _texts(panel)]
    roles = [r['role'] for r in store.messages(panel.active_conversation_id())]
    assert roles == ['user', 'assistant']


def test_turn_order_is_user_then_reply(qapp, panel, store):
    stub = _bind(_MainStub(panel))
    stub.submit_chat_turn('user', 'open youtube')
    stub._log_from_backend('J.A.R.V.I.S.: Opening YouTube now, sir.')
    qapp.processEvents()

    rows = store.messages(panel.active_conversation_id())
    assert [(r['role'], r['text']) for r in rows] == [
        ('user', 'open youtube'),
        ('assistant', 'Opening YouTube now, sir.'),
    ]


def test_no_panel_buffers_the_turn_for_the_first_open_replay(qapp):
    """With no panel yet, the turn rides the buffer the first open renders."""
    stub = _bind(_MainStub(win=None))
    stub.submit_chat_turn('user', 'open youtube')
    assert stub._chat_history == ['You: open youtube'], \
        "an utterance before the panel exists must be buffered for replay"
    assert stub._activity.entries, "the utterance must still reach the activity feed"


def test_submission_never_dispatches_a_command(qapp, panel):
    """The Live turn already executed it — submission must not call the backend."""
    stub = _bind(_MainStub(panel))
    dispatched = []
    stub.on_text_command = lambda text: dispatched.append(text)
    stub.submit_chat_turn('user', 'open youtube')
    qapp.processEvents()
    assert dispatched == [], "submitting the transcript re-dispatched the command"
