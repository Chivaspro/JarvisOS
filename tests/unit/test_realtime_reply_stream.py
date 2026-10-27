"""The reply now streams to the screen and the voice engine while it is written.

The measured problem: in local voice mode (where Fish Audio lives) the reply
text arrived as fragments into a buffer and was only spoken at turn end - so
the user paid the whole generation time before hearing anything, and the text
landed on screen in one block. These tests pin the wiring that removed that
wait, and the properties that must survive it:

* fragments go to the UI and the speech sink AS THEY ARRIVE, in order,
* a cumulative transcript is never displayed or spoken twice,
* the turn is closed exactly once, and a turn that streamed is not logged
  again through the whole-reply path (no double row, no double record),
* the startup greeting is never streamed on top of the copy already shown and
  spoken at startup,
* with no live sink the ordinary whole-reply path keeps the voice.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")


class FakeSink:
    def __init__(self):
        self.fed: list[str] = []
        self.flushes = 0
        self.interrupts = 0
        self.prepares: list[float] = []
        self.speaking = False

    def feed(self, text):
        self.fed.append(str(text))
        return 1

    def flush(self):
        self.flushes += 1
        return 1

    def interrupt(self, log=True):
        self.interrupts += 1
        self.speaking = False
        return 0

    def prepare(self, timeout=6.0):
        self.prepares.append(float(timeout))
        return True

    def is_speaking(self):
        return self.speaking


class FakeUI:
    def __init__(self, panel: bool = True):
        self.streamed: list[str] = []
        self.finished = 0
        self.logged: list[str] = []
        self.flushes = 0
        self.panel = panel
        self._shown = False

    def stream_assistant_text(self, chunk):
        self.streamed.append(str(chunk))

    def flush_assistant_stream_text(self):
        self.flushes += 1
        # A real flush applies the text on the GUI thread; pretend it already
        # did, unless the test wants to observe the wait.
        self._shown = True

    def stream_shown(self):
        return self._shown

    def stream_available(self):
        return self.panel

    def finish_assistant_stream(self):
        self.finished += 1
        self._shown = False

    def write_log(self, text):
        self.logged.append(str(text))


@pytest.fixture(scope="module")
def main():
    return pytest.importorskip("main")


@pytest.fixture(autouse=True)
def _timeline_never_writes_the_project_log(tmp_path):
    """These tests drive real JarvisLive methods, which MARK the latency
    timeline. The timeline must not be polluted with synthetic turns: it is the
    evidence a latency claim is checked against."""
    from voice import turn_timeline
    turn_timeline.configure(tmp_path / "turn_timeline.log")
    yield
    turn_timeline.configure(None)


def _host(main, sink=None, mode="local", panel=True):
    sink = sink if sink is not None else FakeSink()   # never a shared default
    host = main.JarvisLive.__new__(main.JarvisLive)
    host.ui = FakeUI(panel=panel)
    host._speech_stream = sink
    host._spoken = []
    host.speak_local = lambda text, **kw: host._spoken.append(str(text))
    host._stream_gate = ""
    host._stream_buffer = ""
    host._live_out_fed = ""
    host._startup_greeting_text = None
    host.voice_mode = lambda: mode
    host._speech_sink = lambda: sink
    host.set_speaking = lambda value: None
    return host, sink


# ── fragment streaming ───────────────────────────────────────────────────────

def test_fragments_reach_the_ui_and_the_voice_as_they_arrive(main):
    host, sink = _host(main)

    for fragment in ("Good ", "evening, ", "sir."):
        host._stream_reply_text(fragment)

    assert host.ui.streamed == ["Good ", "evening, ", "sir."]
    assert sink.fed == ["Good ", "evening, ", "sir."], (
        "the voice engine must get the same text, in the same order")


def test_a_cumulative_transcript_is_never_streamed_twice(main):
    host, sink = _host(main)

    host._stream_reply_text("Good evening")
    host._stream_reply_text("Good evening, sir.")     # cumulative: prefix repeats

    assert "".join(host.ui.streamed) == "Good evening, sir."
    assert "".join(sink.fed) == "Good evening, sir."


def test_the_turn_is_closed_exactly_once_and_reported_as_streamed(main):
    host, sink = _host(main)
    host._stream_reply_text("Good evening, sir.")

    assert host._finish_reply_stream("Good evening, sir.") is True
    assert host.ui.finished == 1
    assert sink.flushes == 1, "the sink must speak the tail and end the turn"


def test_a_turn_that_never_streamed_is_not_closed_as_one(main):
    host, sink = _host(main)

    assert host._finish_reply_stream("something the model said") is False
    assert host.ui.finished == 0
    assert sink.flushes == 0


def test_without_a_live_sink_nothing_streams_so_the_voice_survives(main):
    host = main.JarvisLive.__new__(main.JarvisLive)
    host.ui = FakeUI()
    host._stream_gate = ""
    host._stream_buffer = ""
    host._live_out_fed = ""
    host._startup_greeting_text = None
    host.voice_mode = lambda: "local"
    host._speech_sink = lambda: None

    host._stream_reply_text("A reply that must still be spoken.")

    assert host.ui.streamed == []
    assert host._finish_reply_stream("A reply that must still be spoken.") is False, (
        "no stream means the caller keeps the whole-reply path (and its voice)")


# ── the startup greeting ─────────────────────────────────────────────────────

def test_the_greeting_is_never_streamed_on_top_of_its_own_copy(main):
    host, sink = _host(main)
    greeting = "Good evening, sir. How may I assist you this evening?"
    host._startup_greeting_text = greeting
    host._reset_reply_stream("voice")

    # The greeting's own transcription arrives while it is already on screen
    # and being spoken: it must NOT be streamed a second time.
    for fragment in ("Good evening, ", "sir. How may I ", "assist you this evening?"):
        host._stream_reply_text(fragment)

    assert host.ui.streamed == []
    assert sink.fed == []

    assert host._finish_reply_stream(greeting) is False
    assert host._startup_greeting_text is None, (
        "the suppression flag is cleared so later turns stream normally")
    assert host.ui.finished == 0
    assert sink.flushes == 0


def test_text_that_diverges_from_the_greeting_is_released_in_full(main):
    host, sink = _host(main)
    greeting = "Good evening, sir. How may I assist you this evening?"
    host._startup_greeting_text = greeting
    host._reset_reply_stream("voice")

    host._stream_reply_text("Good evening, ")
    host._stream_reply_text("sir. The reactor is stable.")     # not the greeting

    joined = "".join(host.ui.streamed)
    assert joined == "Good evening, sir. The reactor is stable.", (
        "the buffered prefix is released, not dropped")
    assert "".join(sink.fed) == joined
    assert host._finish_reply_stream("Good evening, sir. The reactor is stable.") is True


def test_a_typed_turn_is_never_mistaken_for_the_greeting(main):
    host, sink = _host(main)
    host._startup_greeting_text = "Good evening, sir."
    host._reset_reply_stream("text")

    host._stream_reply_text("Two plus two is four.")

    assert host.ui.streamed == ["Two plus two is four."]


# ── the reply is said ONCE (the reported repeat) ─────────────────────────────

def test_a_streamed_turn_is_never_spoken_a_second_time(main):
    """THE reported bug: J.A.R.V.I.S. said the whole reply again when it landed
    in the chat. The turn-end local speech must be skipped for a streamed turn -
    the live sink already spoke the same words, phrase by phrase."""
    host, sink = _host(main)

    # Exactly the turn-end sequence the receive loop runs.
    for fragment in ("Good evening, ", "sir. All systems nominal."):
        host._stream_reply_text(fragment)
    _streamed = host._finish_reply_stream("Good evening, sir. All systems nominal.")
    host._speak_local_turn_reply("Good evening, sir. All systems nominal.", _streamed)

    assert _streamed is True
    assert sink.flushes == 1, "the sink speaks the tail exactly once"
    assert host._spoken == [], (
        "the whole reply must NOT be spoken again after the streamed one")


def test_a_turn_that_did_not_stream_is_still_spoken_by_the_local_engine(main):
    """The fallback must keep working: no stream, so the engine says it."""
    host, sink = _host(main)

    _streamed = host._finish_reply_stream("A reply that never streamed.")
    host._speak_local_turn_reply("A reply that never streamed.", _streamed)

    assert _streamed is False
    assert host._spoken == ["A reply that never streamed."]


def test_an_empty_reply_is_never_spoken(main):
    host, _ = _host(main)
    host._speak_local_turn_reply("   ", False)
    assert host._spoken == []


# ── the first words are readable before they are audible ─────────────────────

def test_the_first_chunk_waits_for_the_text_to_be_shown(main):
    """The UI is coalesced and queued while the voice engine starts at once:
    without the lead the reply is HEARD before it is READ."""
    host, sink = _host(main)
    order: list[str] = []
    host.ui.stream_assistant_text = lambda chunk: order.append("text")
    shown = {"value": False}
    host.ui.stream_shown = lambda: shown["value"]
    kept_feed = sink.feed
    sink.feed = lambda text: (order.append("voice"), kept_feed(text))[1]

    def _show_soon():
        time.sleep(0.05)
        shown["value"] = True

    threading.Thread(target=_show_soon, daemon=True).start()
    host._stream_reply_text("Good evening, sir. ")

    assert host.ui.flushes >= 1, "the UI must be asked to flush immediately"
    assert order and order[0] == "text" and "voice" in order, order


def test_no_panel_means_no_wait_before_speaking(main):
    """Nothing to show must never delay the voice."""
    host, sink = _host(main, panel=False)
    started = time.monotonic()

    host._stream_reply_text("Good evening, sir. ")

    assert time.monotonic() - started < 0.2, (
        "with no chat panel there is nothing to wait for")
    assert sink.fed == ["Good evening, sir. "]


def test_the_lead_gives_up_instead_of_stalling_the_voice(main):
    """A UI that never reports its text must not hold the reply hostage."""
    host, sink = _host(main)
    host.ui.stream_shown = lambda: False            # never becomes readable
    started = time.monotonic()

    host._stream_reply_text("Good evening, sir. ")

    assert time.monotonic() - started < 1.0
    assert sink.fed == ["Good evening, sir. "], "the voice still speaks"


# ── the realtime session is opened early ─────────────────────────────────────

def test_turn_start_prepares_the_realtime_session_in_local_mode(main):
    host, sink = _host(main)

    host._reset_reply_stream("voice")

    assert sink.prepares, (
        "the WebSocket handshake must overlap generation, not delay speech")


def test_native_gemini_audio_never_prepares_the_local_sink(main):
    host, sink = _host(main, mode="gemini")

    host._reset_reply_stream("voice")

    assert sink.prepares == []


# ── main.py wiring (source-level guards) ─────────────────────────────────────

def test_the_receive_loop_streams_the_reply_in_local_mode():
    # The EDGE-PRESERVING fragment, never the trimmed form: the transcription
    # carries its word boundary as a leading space, and streaming the trimmed
    # text welded the words together ("displayed" + " a" -> "displayeda").
    assert "self._stream_reply_text(fragment)" in MAIN_SRC
    assert "self._stream_reply_text(txt)" not in MAIN_SRC
    assert "self._finish_reply_stream(full_out)" in MAIN_SRC


def test_a_streamed_reply_is_not_logged_again():
    """The stream row IS the record: logging the whole text as well would show
    and store the same reply twice."""
    block = MAIN_SRC[MAIN_SRC.index("_streamed = self._finish_reply_stream(full_out)"):]
    assert "elif not _streamed:" in block[:600]


def test_the_turn_end_never_speaks_a_streamed_reply_directly():
    """A bare speak_local(full_out) at turn end is what caused the repeat: the
    decision must go through the streamed-aware helper - the receive loop must
    not be able to speak a whole reply on its own."""
    assert "self._speak_local_turn_reply(full_out, _streamed)" in MAIN_SRC
    assert MAIN_SRC.count("args=(full_out") == 1, (
        "exactly one place may speak a finished reply (the streamed-aware "
        "helper), otherwise the repeat can come back")


def test_interrupt_stops_the_sink_too():
    block = MAIN_SRC[MAIN_SRC.index("def interrupt(self, source"):]
    block = block[:6000]
    assert "_sink.interrupt(log=False)" in block


def test_narration_cannot_talk_over_a_streamed_reply():
    block = MAIN_SRC[MAIN_SRC.index("def _narration_blocked"):]
    assert "_sink.is_speaking()" in block[:900]


def test_a_whole_line_takes_priority_over_the_streamed_reply():
    block = MAIN_SRC[MAIN_SRC.index("def speak_local"):]
    assert "_sink.interrupt(log=False)" in block[:1800]
