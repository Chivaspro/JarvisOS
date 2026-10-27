"""A streamed reply must stay live on screen WITHOUT pegging the GUI thread.

The backend now hands the assistant's text over token by token. Doing the
obvious thing - one widget update per token - is the classic way a stream
freezes a window. These tests drive the real ``MainWindow`` offscreen and pin:

* the text is coalesced into a bounded number of updates, and the reply is
  still complete to the character,
* closing the stream records the reply ONCE (store + bubble), with the tail
  that arrived after the last flush,
* with no chat panel the reply still reaches the legacy surfaces, exactly as
  the whole-reply ``write_log`` path would have done.
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

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_streamed_reply", ROOT / "ui.py")
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


def _pump(qapp, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def _rows(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatMessageRow)]


def _open_panel(qapp, win, timeout: float = 15.0):
    """Open the Chat Panel and return it.

    The panel is deliberately not created while the startup reactor is still
    gliding (the toggle is queued), so this waits for the REAL open rather than
    asserting a panel that the app has not created yet.
    """
    win._toggle_command_window()
    deadline = time.monotonic() + timeout
    while win._command_window is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    panel = win._command_window
    assert panel is not None, "the chat panel must open"
    qapp.processEvents()
    return panel


# ── coalescing ───────────────────────────────────────────────────────────────

def test_tokens_are_coalesced_but_the_reply_arrives_complete(qapp, win):
    panel = _open_panel(qapp, win)
    assert panel is not None

    updates: list[str] = []
    original = panel.append_assistant_stream
    panel.append_assistant_stream = lambda chunk: (updates.append(chunk),
                                                   original(chunk))[1]

    # A realistic stream: 120 tokens arriving as fast as the provider sends them.
    tokens = [f"word{i} " for i in range(120)]
    for token in tokens:
        win.stream_assistant_text(token)
    _pump(qapp, 0.6)
    win.finish_assistant_stream()
    _pump(qapp, 0.3)

    joined = "".join(updates)
    assert joined == "".join(tokens), "no token may be lost or reordered"
    assert len(updates) < len(tokens), (
        "the GUI must not be updated once per token")
    assert len(updates) <= 8, f"a 120-token stream produced {len(updates)} updates"


def test_the_final_text_is_recorded_once_with_its_tail(qapp, win, store):
    panel = _open_panel(qapp, win)

    win.stream_assistant_text("Good evening, sir. ")
    win.stream_assistant_text("All systems are nominal.")
    win.finish_assistant_stream()
    _pump(qapp, 0.3)

    rows = store.messages(panel.active_conversation_id())
    assert [r['text'] for r in rows] == ["Good evening, sir. All systems are nominal."], (
        "the streamed reply is stored exactly once, complete and in order")
    bubbles = [r._bubble._raw_text for r in _rows(panel)]
    assert bubbles == ["Good evening, sir. All systems are nominal."]


def test_a_second_turn_streams_as_its_own_row(qapp, win, store):
    panel = _open_panel(qapp, win)

    win.stream_assistant_text("First reply.")
    win.finish_assistant_stream()
    _pump(qapp, 0.2)
    win.stream_assistant_text("Second reply.")
    win.finish_assistant_stream()
    _pump(qapp, 0.2)

    rows = store.messages(panel.active_conversation_id())
    assert [r['text'] for r in rows] == ["First reply.", "Second reply."]


# ── the no-panel case ────────────────────────────────────────────────────────

def test_a_streamed_reply_is_visible_even_with_no_chat_panel(qapp, win):
    assert win._command_window is None, "the HUD starts compact"

    win.stream_assistant_text("A reply with nowhere obvious to go.")
    win.finish_assistant_stream()
    _pump(qapp, 0.3)

    legacy = list(getattr(win, '_chat_history', []) or [])
    assert any("A reply with nowhere obvious to go." in entry for entry in legacy), (
        "without a panel the reply must still reach the transport buffer the "
        "first-open replay renders")

    activity = win.get_activity_lines(40)
    assert any("A reply with nowhere obvious to go." in line for line in activity), (
        "the activity feed must show the reply, like write_log did")


# ── word boundaries survive the whole display path ───────────────────────────

def _rendered_text(row) -> str:
    """What the bubble actually shows, as plain text (Markdown applied)."""
    body = getattr(row._bubble, '_body', None)
    if body is None:
        return ""
    for attr in ("toPlainText", "text"):
        fn = getattr(body, attr, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:
                pass
    return ""


def test_reported_welded_words_render_with_their_spaces(qapp, win, store):
    """The exact reported message, streamed the way the Live session sends it:
    'I have displayeda comprehensiveF2L algorithmcheat sheet for you, sir.'"""
    panel = _open_panel(qapp, win)
    fragments = ["I have displayed", " a comprehensive", " F2L", " algorithm",
                 " cheat sheet for you, sir."]

    for fragment in fragments:
        win.stream_assistant_text(fragment)
    win.finish_assistant_stream()
    _pump(qapp, 0.3)

    expected = "I have displayed a comprehensive F2L algorithm cheat sheet for you, sir."
    rows = _rows(panel)
    assert [r._bubble._raw_text for r in rows] == [expected]
    assert expected in _rendered_text(rows[0]), _rendered_text(rows[0])
    assert [r['text'] for r in store.messages(panel.active_conversation_id())] == \
        [expected], "the stored reply must keep the spaces too"


def test_a_normal_streamed_reply_is_untouched_by_the_fix(qapp, win, store):
    """No invented spaces: a reply streamed with its own spacing renders as-is."""
    panel = _open_panel(qapp, win)
    sentence = "Sir, the layout is fixed, and the F2L sheet is open."

    win.stream_assistant_text(sentence)
    win.finish_assistant_stream()
    _pump(qapp, 0.3)

    assert [r._bubble._raw_text for r in _rows(panel)] == [sentence]
    assert [r['text'] for r in store.messages(panel.active_conversation_id())] == \
        [sentence]


# ── readable before audible ──────────────────────────────────────────────────

def test_the_pending_text_can_be_applied_immediately(qapp, win):
    """The backend holds the first spoken word until the reply is readable, so
    it needs a flush that does not wait for the coalescing tick."""
    panel = _open_panel(qapp, win)

    win.stream_assistant_text("Readable at once, sir.")
    win.flush_assistant_stream_text()
    qapp.processEvents()

    assert win.stream_shown() is True
    assert [r._bubble._raw_text for r in _rows(panel)] == ["Readable at once, sir."]


def test_stream_shown_resets_for_the_next_turn(qapp, win):
    _open_panel(qapp, win)
    win.stream_assistant_text("First.")
    win.flush_assistant_stream_text()
    qapp.processEvents()
    assert win.stream_shown() is True

    win.finish_assistant_stream()
    _pump(qapp, 0.2)

    assert win.stream_shown() is False, (
        "the next turn must lead with its own text, not inherit this one")


def test_with_no_panel_the_reply_is_readable_while_it_is_spoken(qapp, win):
    """The reported experience: J.A.R.V.I.S. speaking while nothing at all is
    on screen. With the panel closed the first chunk becomes an activity line."""
    assert win._command_window is None
    assert win.stream_available() is False

    win.stream_assistant_text("A reply that must be visible while it is spoken.")
    win.flush_assistant_stream_text()
    qapp.processEvents()

    assert win.stream_shown() is True
    activity = win.get_activity_lines(40)
    assert any("must be visible while it is spoken" in line for line in activity), (
        "the reply must be readable somewhere while it is being said")


def test_the_activity_preview_is_only_the_first_chunk(qapp, win):
    """A preview per flush would spam the feed; one per turn is a heads-up."""
    _pump(qapp, 0.0)
    for chunk in ("First part. ", "Second part. ", "Third part. "):
        win.stream_assistant_text(chunk)
        win.flush_assistant_stream_text()
    qapp.processEvents()

    previews = [line for line in win.get_activity_lines(60)
                if "First part." in line or "Second part." in line
                or "Third part." in line]
    assert len(previews) == 1, previews


def test_an_empty_stream_leaves_no_empty_row(qapp, win, store):
    panel = _open_panel(qapp, win)

    win.finish_assistant_stream()
    _pump(qapp, 0.2)

    assert store.messages(panel.active_conversation_id()) == []
    assert _rows(panel) == []
