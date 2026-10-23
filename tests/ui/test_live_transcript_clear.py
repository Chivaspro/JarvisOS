"""The voice transcript must leave the input box when the utterance is sent.

Two user-visible failures are pinned here.

1. **Stale text.** The realtime transcript is rendered into the ONE existing
   command textbox. When the turn ended, the text was deliberately left
   visible, so saying "hello" sent the command, wrote "You: hello" to the Chat
   Panel — and left "hello" sitting in the input field, looking unsent and
   inviting a stray Enter to send it a second time.

2. **The next utterance losing its transcript.** Worse than cosmetic: the field
   keeps focus, so leftover text from the previous turn was indistinguishable
   from a hand-typed draft. The draft guard then refused to write anything for
   the NEXT turn, and live transcription silently stopped appearing until the
   user manually cleared the box.

The fix compares the field against what the transcript actually echoed, so a
real draft is still protected while turn leftovers are not.

The real bound methods are exercised through ``_CommandWindow`` (loaded by the
same path-alias harness the other HUD tests use) with a real ``QLineEdit``, so
nothing here is a re-implementation of the logic under test.
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

from PyQt6.QtWidgets import QLineEdit, QWidget  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_spec = importlib.util.spec_from_file_location("ui_impl_live_transcript_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)


class _FocusBox(QLineEdit):
    """A line edit whose focus state the test decides.

    ``QLineEdit.hasFocus()`` depends on a shown, active window, which an
    offscreen test does not have — so the transcript's draft detection could
    never be exercised. The real code calls ``box.hasFocus()``; overriding it
    here keeps that call going through the production path.
    """

    def __init__(self, focused: bool = False, text: str = ""):
        super().__init__(text)
        self._focused = bool(focused)

    def hasFocus(self) -> bool:  # noqa: N802 - Qt naming
        return self._focused


def _command_window(box: QLineEdit) -> QWidget:
    """A bare stub carrying only what ``set_live_transcript`` touches."""
    stub = QWidget()
    stub.input = box
    for name in ('set_live_transcript', '_bind_speech_composer',
                 '_on_composer_user_edit'):
        setattr(stub, name, getattr(ui._CommandWindow, name).__get__(stub, QWidget))
    # Wire the real textEdited tracker so a user edit is detected the same way
    # the production window detects it.
    stub._bind_speech_composer(box)
    return stub


def _speak(stub, *chunks, final: bool = False):
    """Feed one turn's chunks; the last one is optionally the final result."""
    for index, chunk in enumerate(chunks):
        stub.set_live_transcript(chunk, final=final and index == len(chunks) - 1)


# ── The field shows the live transcript while the user speaks ──────────────

def test_interim_transcript_fills_the_input_box(qapp):
    box = _FocusBox(focused=False)
    window = _command_window(box)

    _speak(window, "hello")

    assert box.text() == "hello"


def test_interim_transcripts_replace_rather_than_append(qapp):
    box = _FocusBox(focused=False)
    window = _command_window(box)

    _speak(window, "hello", "hello there")

    assert box.text() == "hello there"


# ── Regression 1: sent text leaves the box ────────────────────────────────

def test_field_is_cleared_when_the_utterance_is_sent(qapp):
    box = _FocusBox(focused=False)
    window = _command_window(box)

    _speak(window, "hello", final=True)

    assert box.text() == "", "a sent utterance must not stay in the input field"


def test_field_is_cleared_even_when_the_final_text_is_corrected(qapp):
    """The final result can differ from the interim (speech correction)."""
    box = _FocusBox(focused=False)
    window = _command_window(box)

    window.set_live_transcript("open web brow")
    window.set_live_transcript("Open Webcam", final=True)

    assert box.text() == ""


def test_canceled_turn_clears_the_echoed_transcript(qapp):
    """An interrupted utterance is closed with an empty final transcript."""
    box = _FocusBox(focused=False)
    window = _command_window(box)

    window.set_live_transcript("hello")
    window.set_live_transcript("", final=True)

    assert box.text() == ""


# ── Regression 2: the NEXT utterance still renders ────────────────────────

def test_next_utterance_still_renders_into_the_focused_box(qapp):
    """The field normally keeps focus; that must not read as a user draft."""
    box = _FocusBox(focused=True)
    window = _command_window(box)

    _speak(window, "hello", final=True)          # turn one: sent + cleared
    _speak(window, "what is the weather")        # turn two: must appear

    assert box.text() == "what is the weather"


def test_text_present_before_the_turn_is_left_alone(qapp):
    """Undecidable by design, so the safe choice is made: do not destroy it.

    A finished turn now clears whatever it echoed, so text already in the field
    when a turn STARTS can only have been put there by the user (or by another
    feature) — indistinguishable from a draft, and therefore protected. The
    "leftover suppressed the next transcript" failure cannot reach this state
    any more; that path is covered by the completed-turn test above.
    """
    box = _FocusBox(focused=True, text="hello")
    window = _command_window(box)

    window.set_live_transcript("hello")
    window.set_live_transcript("hello", final=True)

    assert box.text() == "hello"


# ── A hand-typed draft is still protected ────────────────────────────────

def test_user_draft_is_not_overwritten_by_speech(qapp):
    box = _FocusBox(focused=True, text="draft I was typing")
    window = _command_window(box)

    _speak(window, "hello")

    assert box.text() == "draft I was typing"


def test_user_draft_is_not_cleared_when_the_turn_ends(qapp):
    box = _FocusBox(focused=True, text="draft I was typing")
    window = _command_window(box)

    _speak(window, "hello", final=True)

    assert box.text() == "draft I was typing"


def test_an_edit_made_after_the_transcript_is_never_destroyed(qapp):
    """The user correcting the recognised text keeps their correction."""
    box = _FocusBox(focused=False)
    window = _command_window(box)

    window.set_live_transcript("open chrome")
    box.setText("open chrome and search for hotels")   # user edited it
    window.set_live_transcript("", final=True)

    assert box.text() == "open chrome and search for hotels"


def test_a_new_turn_after_a_draft_is_still_protected(qapp):
    """Draft protection is per turn, and it resets cleanly afterwards."""
    box = _FocusBox(focused=True, text="my draft")
    window = _command_window(box)

    _speak(window, "hello", final=True)
    assert box.text() == "my draft"

    box.clear()                                   # user emptied it themselves
    _speak(window, "second utterance")
    assert box.text() == "second utterance"


# ── A leftover ECHO from an earlier turn is never mistaken for a draft ─────

def test_a_leftover_echo_does_not_suppress_the_next_utterance(qapp):
    """The reported failure: stale transcript text blocked the next live view.

    The text left in the box is one WE echoed earlier. It is recognised as
    ours (not a user draft), so the next utterance renders and is cleared
    normally — no stale command is stranded in the composer.
    """
    box = _FocusBox(focused=True)
    window = _command_window(box)

    window.set_live_transcript("open youtube")        # turn one echoes it
    box.setText("open youtube")                       # the leftover the bug left
    window.set_live_transcript("what is the weather")  # turn two must render
    assert box.text() == "what is the weather", \
        "a leftover echo was treated as a draft and suppressed the transcript"

    window.set_live_transcript("", final=True)
    assert box.text() == "", "the echoed transcript must not survive the turn"


def test_a_late_duplicate_fragment_cannot_strand_text(qapp):
    """Even a straggler arriving after the turn closes self-heals."""
    box = _FocusBox(focused=False)
    window = _command_window(box)

    window.set_live_transcript("open youtube", final=True)
    assert box.text() == ""

    window.set_live_transcript("open youtube")   # straggler opens a "turn"
    window.set_live_transcript("", final=True)   # and is closed normally
    assert box.text() == ""


def test_an_empty_final_transcript_leaves_no_trace(qapp):
    box = _FocusBox(focused=False)
    window = _command_window(box)
    window.set_live_transcript("", final=True)
    assert box.text() == ""


# ── An edit DURING the turn is flagged through textEdited ─────────────────

def test_an_edit_during_the_turn_is_flagged_as_a_draft(qapp):
    """``textEdited`` (real typing only) protects a mid-turn correction."""
    box = _FocusBox(focused=False)
    window = _command_window(box)

    window.set_live_transcript("open chrome")
    # The user corrects the recognition result by hand.
    box.textEdited.emit("open chrome and search for hotels")
    box.setText("open chrome and search for hotels")

    window.set_live_transcript("open chrome and search")   # interim must not clobber
    assert box.text() == "open chrome and search for hotels"

    window.set_live_transcript("", final=True)
    assert box.text() == "open chrome and search for hotels", \
        "the user's own correction must survive the turn"
