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
    def __init__(self):
        self.streamed: list[str] = []
        self.finished = 0
        self.logged: list[str] = []

    def stream_assistant_text(self, chunk):
        self.streamed.append(str(chunk))

    def finish_assistant_stream(self):
        self.finished += 1

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


def _host(main, sink=None, mode="local"):
    sink = sink if sink is not None else FakeSink()   # never a shared default
    host = main.JarvisLive.__new__(main.JarvisLive)
    host.ui = FakeUI()
    host._speech_stream = sink
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
    assert "self._stream_reply_text(txt)" in MAIN_SRC
    assert "self._finish_reply_stream(full_out)" in MAIN_SRC


def test_a_streamed_reply_is_not_logged_again():
    """The stream row IS the record: logging the whole text as well would show
    and store the same reply twice."""
    block = MAIN_SRC[MAIN_SRC.index("_streamed = self._finish_reply_stream(full_out)"):]
    assert "elif not _streamed:" in block[:600]


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
