"""The live speech sink: speak the reply WHILE it is still being generated.

These tests pin the properties the realtime pipeline depends on, with
deterministic fakes - no network, no audio device:

* text is spoken at sentence boundaries while the turn is still OPEN (the
  whole point: TTS must not wait for the finished reply),
* a delta transcript and a cumulative transcript both speak every word exactly
  once,
* an interrupted turn can never resume, and the engine is actually cancelled,
* a full queue drops instead of stalling the caller, and
* a failing renderer costs the voice, never the worker or the conversation.
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

from voice.speech_stream import StreamingSpeechService  # noqa: E402


class FakePlayer:
    """Stands in for voice.tts.TTSPlayer (same speak() signature)."""

    def __init__(self, name="edgetts", gate: threading.Event | None = None,
                 fail_first: bool = False):
        self._name = name
        self.spoken: list[str] = []
        self.cancelled: list[bool] = []
        self.audio_starts = 0
        self._gate = gate
        self._fail_first = fail_first
        self._calls = 0

    def speak(self, text, on_start=None, on_done=None, cancel=None, on_audio=None):
        self._calls += 1
        if self._fail_first and self._calls == 1:
            raise RuntimeError("engine exploded")
        if cancel is not None and cancel.is_set():
            return
        self.spoken.append(str(text))
        self.cancelled.append(bool(cancel is not None and cancel.is_set()))
        if on_audio is not None:
            self.audio_starts += 1
            on_audio()
        if self._gate is not None:
            self._gate.wait(timeout=2.0)


class FakeFishSession:
    """Stands in for the Fish realtime WebSocket session.

    Consumes the text iterator the way the SDK's sender thread does and reports
    "first audio" as soon as the first chunk has been sent - which is exactly
    what must happen before the turn's text has finished.
    """

    def __init__(self, fail_after: int | None = None):
        self.chunks: list[str] = []
        self.audio_starts = 0
        self.cancelled = False
        self.sessions = 0
        self.started = threading.Event()
        self.finished = threading.Event()
        self._fail_after = fail_after

    def __call__(self, text_iter, cancel=None, cfg=None, player=None, on_audio=None):
        self.sessions += 1
        self.started.set()
        try:
            for chunk in text_iter:
                if cancel is not None and cancel.is_set():
                    self.cancelled = True
                    return
                self.chunks.append(str(chunk))
                if self._fail_after is not None and len(self.chunks) > self._fail_after:
                    raise RuntimeError("websocket dropped")
                if on_audio is not None:
                    self.audio_starts += 1
                    on_audio()
        finally:
            self.finished.set()


def _service(**kwargs):
    player = kwargs.pop("player", None) or FakePlayer()
    return StreamingSpeechService({}, player=player, **kwargs), player


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ── realtime intake ──────────────────────────────────────────────────────────

def test_a_sentence_is_spoken_while_the_turn_is_still_open():
    service, player = _service(realtime=False)

    service.feed("Good evening, sir. ")          # first sentence complete
    assert _wait(lambda: player.spoken == ["Good evening, sir."]), player.spoken

    # The turn is NOT over: the next sentence has not even arrived yet.
    assert service.phase() in ("synthesizing", "playing", "idle")
    service.feed("How may I assist you this evening?")
    assert _wait(lambda: len(player.spoken) == 2), player.spoken
    assert player.spoken[1] == "How may I assist you this evening?"


def test_flush_speaks_the_unpunctuated_tail_and_resets_the_turn():
    service, player = _service(realtime=False)

    service.feed("Yes indeed")
    assert player.spoken == []                    # nothing to cut on yet
    service.flush()

    assert _wait(lambda: player.spoken == ["Yes indeed"]), player.spoken
    # The next turn starts clean: the feed buffer was reset, so a sentence that
    # repeats the previous turn's words is still spoken.
    service.feed("Yes indeed, and no mistake.")
    assert _wait(lambda: len(player.spoken) == 2), player.spoken
    assert player.spoken[1] == "Yes indeed, and no mistake."


def test_delta_fragments_speak_every_word_exactly_once():
    service, player = _service(realtime=False)

    for fragment in ("Sir,", " the", " core", " is", " stable.", " All", " good."):
        service.feed(fragment)
    service.flush()

    assert _wait(lambda: len(player.spoken) == 2), player.spoken
    assert " ".join(player.spoken) == "Sir, the core is stable. All good."


def test_a_cumulative_transcript_speaks_every_word_exactly_once():
    """Gemini's transcription arrives as deltas in some SDK versions and as a
    growing string in others; both must be safe (the reported bug: the first
    sentence spoken twice because the suffix logic was missing)."""
    service, player = _service(realtime=False)

    service.feed("Good evening")
    service.feed("Good evening, sir.")            # cumulative: same prefix again
    service.flush()

    assert _wait(lambda: player.spoken), player.spoken
    assert " ".join(player.spoken) == "Good evening, sir."


def test_a_repeated_fragment_is_never_spoken_twice():
    service, player = _service(realtime=False)

    service.feed("One moment please.")
    service.feed("One moment please.")             # a duplicate event
    service.flush()

    assert _wait(lambda: player.spoken), player.spoken
    assert player.spoken == ["One moment please."]


def test_ordering_is_preserved_across_many_chunks():
    service, player = _service(realtime=False)
    chunks = [f"Sentence number {i}." for i in range(12)]

    for chunk in chunks:
        service.feed(chunk + " ")
    service.flush()

    assert _wait(lambda: len(player.spoken) == 12), player.spoken
    assert player.spoken == chunks


# ── cancellation ─────────────────────────────────────────────────────────────

def test_interrupt_stops_the_current_line_and_drops_the_rest():
    gate = threading.Event()
    service, player = _service(player=FakePlayer(gate=gate), realtime=False)

    service.feed("First line here. Second line here. Third line here.")
    assert _wait(lambda: player.spoken), player.spoken      # the first line is playing
    cancel_event = service._cancel

    cleared = service.interrupt()

    assert cleared >= 1                       # the queued lines are gone
    assert cancel_event.is_set(), "the engine must be cancelled, not just ignored"
    gate.set()                                 # let the blocked line return
    time.sleep(0.1)
    assert player.spoken == ["First line here."], (
        "nothing queued before the interrupt may start after it")
    assert service.phase() == "idle"


def test_nothing_resumes_after_an_interrupt_even_if_text_still_arrives():
    service, player = _service(realtime=False)
    service.interrupt()

    service.feed("This belongs to the interrupted turn.")
    service.flush()

    assert _wait(lambda: player.spoken), player.spoken


# ── bounds and failure ───────────────────────────────────────────────────────

def test_a_full_queue_drops_instead_of_stalling_the_caller():
    gate = threading.Event()
    service, player = _service(player=FakePlayer(gate=gate), realtime=False,
                               max_queue=2)

    started = time.monotonic()
    for i in range(40):
        service.feed(f"Chunk number {i} is long enough to count.")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the Live receive loop must never block on the voice"
    assert service.status()["dropped"] > 0
    gate.set()


def test_a_failing_renderer_never_kills_the_worker():
    service, player = _service(player=FakePlayer(fail_first=True), realtime=False)

    service.feed("This one raises. ")
    service.feed("This one must still be spoken. ")
    service.flush()

    assert _wait(lambda: player.spoken == ["This one must still be spoken."]), player.spoken
    assert "engine exploded" in service.status()["reason"]


# ── the Fish realtime backend ────────────────────────────────────────────────

def test_fish_backend_streams_text_before_the_turn_finishes():
    session = FakeFishSession()
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session)

    service.feed("Good evening, sir. ")            # first sentence complete

    assert session.started.wait(timeout=2.0)
    assert _wait(lambda: session.chunks == ["Good evening, sir."]), session.chunks
    assert session.audio_starts >= 1, "audio must be produced before the turn ends"
    assert not session.finished.is_set(), "the session must still be open mid-turn"

    service.feed("How may I assist you this evening?")
    service.flush()

    assert _wait(lambda: session.finished.is_set())
    assert session.chunks == ["Good evening, sir.", "How may I assist you this evening?"]
    assert service.status()["backend"] == "fish"


def test_fish_session_failure_reports_and_hands_the_rest_to_the_engine():
    session = FakeFishSession(fail_after=1)
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session)

    service.feed("First sentence arrives. ")       # session dies on the next chunk
    service.feed("Second sentence arrives. ")
    service.feed("Third sentence arrives. ")
    service.flush()

    assert _wait(lambda: "websocket dropped" in service.status()["reason"])
    assert _wait(lambda: player.spoken, timeout=3.0), (
        "the rest of the turn must still be spoken through the engine path")
    assert service.status()["backend"] == "engine"


def test_prepare_opens_the_session_before_any_text_exists():
    """The handshake (~0.6-0.9 s measured) must overlap generation, not delay
    the first phrase: the socket is opened when the TURN starts."""
    session = FakeFishSession()
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session)

    assert service.prepare() is True
    assert session.started.wait(timeout=2.0)
    assert session.chunks == [], "opened early: no text has been produced yet"

    service.feed("Now the first sentence arrives. ")
    assert _wait(lambda: session.chunks == ["Now the first sentence arrives."])
    service.flush()
    assert _wait(lambda: session.finished.is_set())
    assert session.sessions == 1, "one session for the whole turn"


def test_prepare_closes_an_idle_session_and_the_next_turn_opens_a_fresh_one():
    session = FakeFishSession()
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session)

    assert service.prepare(timeout=1.0) is True
    assert _wait(lambda: session.finished.is_set(), timeout=3.0), (
        "a prepared session with no text must close itself")

    service.feed("This turn does speak. ")
    service.flush()

    assert _wait(lambda: session.sessions == 2), session.sessions
    assert session.chunks == ["This turn does speak."]


def test_prepare_is_a_no_op_when_realtime_is_off_or_not_fish():
    session = FakeFishSession()
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session, realtime=False)
    assert service.prepare() is False
    assert session.sessions == 0

    session2 = FakeFishSession()
    service2, _ = _service(player=FakePlayer(name="edgetts"),
                           session_factory=session2)
    assert service2.prepare() is False
    assert session2.sessions == 0


def test_text_taken_by_a_session_that_never_spoke_is_still_spoken():
    """A socket that dies BEFORE any audio must not swallow the reply: the text
    it took - plus what was still queued - is re-rendered by the engine."""

    class _NoAudioSession(FakeFishSession):
        def __call__(self, text_iter, cancel=None, cfg=None, player=None,
                     on_audio=None):
            self.sessions += 1
            self.started.set()
            try:
                for index, chunk in enumerate(text_iter):
                    self.chunks.append(str(chunk))
                    if index >= 1:
                        raise RuntimeError("socket closed before any audio")
            finally:
                self.finished.set()

    session = _NoAudioSession()
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session)

    service.feed("First sentence arrives. ")
    service.feed("Second sentence arrives. ")
    service.flush()

    assert _wait(lambda: player.spoken, timeout=3.0), (
        "nothing was heard from the socket, so the reply must still be spoken")
    assert " ".join(player.spoken) == "First sentence arrives. Second sentence arrives."
    assert service.status()["backend"] == "engine"
    assert "socket closed" in service.status()["reason"]


def test_a_session_that_spoke_is_never_replayed_on_failure():
    """The mirror image: once audio was heard, the same words must not repeat."""
    session = FakeFishSession(fail_after=1)          # on_audio fires, then it dies
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session)

    service.feed("First sentence arrives. ")
    service.feed("Second sentence arrives. ")
    service.flush()

    assert _wait(lambda: "websocket dropped" in service.status()["reason"])
    time.sleep(0.2)
    assert player.spoken == [], "already-heard speech must not be re-spoken"


def test_realtime_off_uses_the_engine_path_even_for_a_fish_voice():
    session = FakeFishSession()
    service, player = _service(player=FakePlayer(name="fish_audio"),
                               session_factory=session, realtime=False)

    service.feed("Spoken the ordinary way. ")
    service.flush()

    assert _wait(lambda: player.spoken == ["Spoken the ordinary way."]), player.spoken
    assert session.chunks == [], "the WebSocket must not be used when realtime is off"


def test_a_fish_player_without_a_session_falls_back_to_the_engine():
    """A build where the realtime session is unavailable must still speak."""
    service, player = _service(player=FakePlayer(name="fish_audio"))

    service.feed("Still spoken, just not over the socket. ")
    service.flush()

    assert _wait(lambda: player.spoken == ["Still spoken, just not over the socket."])
    assert service.status()["backend"] == "engine"


# ── state + lifecycle ────────────────────────────────────────────────────────

def test_state_callbacks_report_playback_and_idle():
    phases: list[str] = []
    service, player = _service(realtime=False)
    service.on_state(phases.append)

    service.feed("A complete sentence. ")
    assert _wait(lambda: "playing" in phases), phases
    assert _wait(lambda: service.phase() == "idle")

    assert phases[-1] == "idle"


def test_shutdown_silences_the_stream_and_joins_the_workers():
    service, player = _service(realtime=False)
    service.feed("Something is being said. ")
    assert _wait(lambda: player.spoken)

    service.shutdown()

    assert service.phase() == "idle"
    assert not service.is_speaking()


def test_speak_submits_a_whole_line_including_its_tail():
    service, player = _service(realtime=False)

    service.speak("Corrected reply without a final period")

    assert _wait(lambda: player.spoken == ["Corrected reply without a final period"])


def test_status_never_leaks_the_reply_text():
    service, player = _service(realtime=False)
    service.feed("A private sentence that must not appear in diagnostics. ")

    assert _wait(lambda: player.spoken)
    assert "private" not in str(service.status())
