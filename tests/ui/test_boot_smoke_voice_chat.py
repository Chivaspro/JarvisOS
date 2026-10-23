"""Boot smoke: the real MainWindow drives a full voice turn end to end.

The live Gemini session cannot run headless, so this exercises every layer the
voice pipeline touches once the transcript is known: the window's submission
API (``submit_chat_turn``), the chat panel's row rendering, the conversation
store, and the restore-scroll path — against a real ``MainWindow`` built
offscreen, not a stub.
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
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_boot_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from memory import conversations

    instance = conversations.ConversationStore(tmp_path / "conversations.db")
    monkeypatch.setattr(conversations, "get_store", lambda *a, **k: instance)
    yield instance
    instance.close()


@pytest.fixture()
def win(qapp, tmp_path, store):
    window = ui.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()
    yield window
    try:
        panel = getattr(window, '_command_window', None)
        if panel is not None:
            panel.close()
    except Exception:
        pass
    try:
        window.close()
    except Exception:
        pass
    qapp.processEvents()


def _rows(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatMessageRow)]


def test_a_voice_turn_becomes_a_recorded_chat_turn(qapp, win, store):
    win._toggle_command_window()
    qapp.processEvents()
    panel = win._command_window
    assert panel is not None

    win.submit_chat_turn('user', 'Jarvis, open YouTube')
    win.write_log('J.A.R.V.I.S.: Opening YouTube now, sir.')
    qapp.processEvents()

    rows = store.messages(panel.active_conversation_id())
    assert [(r['role'], r['text']) for r in rows] == [
        ('user', 'Jarvis, open YouTube'),
        ('assistant', 'Opening YouTube now, sir.'),
    ]
    assert 'Jarvis, open YouTube' in [r._bubble._raw_text for r in _rows(panel)]


def test_a_voice_turn_is_recorded_even_with_the_panel_hidden(qapp, win, store):
    win._toggle_command_window()
    qapp.processEvents()
    panel = win._command_window
    # Establish a conversation so the id is stable.
    panel.append_msg('J.A.R.V.I.S.', 'At your service.', you=False)
    conv_id = panel.active_conversation_id()

    panel.hide()
    qapp.processEvents()
    assert panel.isVisible() is False

    win.submit_chat_turn('user', 'open youtube')
    qapp.processEvents()

    rows = store.messages(conv_id)
    assert rows[-1]['role'] == 'user'
    assert rows[-1]['text'] == 'open youtube'


def test_opening_a_conversation_lands_at_the_newest_message(qapp, win, store):
    conv = store.new_conversation()['id']
    for i in range(30):
        store.add_message(conv, 'user' if i % 2 == 0 else 'assistant',
                          f'history line {i} ' + 'x' * 60)

    win._toggle_command_window()
    qapp.processEvents()
    panel = win._command_window
    assert panel.load_conversation(conv) is True
    qapp.processEvents()
    qapp.processEvents()

    bar = panel.chat.verticalScrollBar()
    assert bar.maximum() > 0, "the restored conversation must overflow"
    assert bar.value() == bar.maximum(), \
        "opening a previous conversation must show its newest message"


def test_switching_between_conversations_stays_at_the_newest(qapp, win, store):
    first = store.new_conversation()['id']
    second = store.new_conversation()['id']
    for i in range(20):
        store.add_message(first, 'user', f'first {i} ' + 'x' * 60)
        store.add_message(second, 'user', f'second {i} ' + 'x' * 60)

    win._toggle_command_window()
    qapp.processEvents()
    panel = win._command_window

    for conv in (first, second, first):
        assert panel.load_conversation(conv) is True
        qapp.processEvents()
        qapp.processEvents()
        bar = panel.chat.verticalScrollBar()
        assert bar.value() == bar.maximum(), \
            f"switching to {conv} did not land on its newest message"
