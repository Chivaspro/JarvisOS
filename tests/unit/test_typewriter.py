"""Time-based typewriter: exact text, continuous reveal, no voice coupling."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui_typewriter import CharPacer, TypewriterReveal  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def _drain(pacer: CharPacer, clock: FakeClock, limit: int = 5000) -> str:
    out = []
    for _ in range(limit):
        clock.advance(0.016)
        out.append(pacer.tick())
        if pacer.done:
            break
    return "".join(out)


def test_exact_text_is_preserved_across_chunks():
    clock = FakeClock()
    pacer = CharPacer(cps=110, clock=clock)
    pieces = ["Hello", " world.", "\n\nSecond **paragraph**", " here.\n"]
    revealed = []
    for piece in pieces:
        pacer.push(piece)
        for _ in range(20):
            clock.advance(0.016)
            revealed.append(pacer.tick())
    pacer.finish()
    revealed.append(_drain(pacer, clock))
    assert "".join(revealed) == "".join(pieces)


def test_every_character_appears_exactly_once():
    clock = FakeClock()
    text = "The quick brown fox jumps over the lazy dog. 0123456789!"
    pacer = CharPacer(cps=140, clock=clock)
    pacer.push(text)
    pacer.finish()
    out = _drain(pacer, clock)
    assert out == text
    assert len(out) == len(text)
    assert out.count("q") == 1
    assert out.count(" ") == text.count(" ")


def test_markdown_and_paragraphs_survive_verbatim():
    clock = FakeClock()
    text = "# Title\n\n- one\n- two\n\n```python\nprint('x')\n```\n"
    pacer = CharPacer(cps=200, clock=clock)
    pacer.push(text)
    pacer.finish()
    assert _drain(pacer, clock) == text


def test_flush_releases_everything_hidden_and_can_repeat():
    clock = FakeClock()
    pacer = CharPacer(cps=30, clock=clock)
    pacer.push("unfinished sentence")
    clock.advance(0.02)
    pacer.tick()
    rest = pacer.flush()
    assert rest == "nfinished sentence"
    assert pacer.flush() == ""
    assert pacer.idle


def test_finished_reply_drains_promptly():
    clock = FakeClock()
    pacer = CharPacer(cps=60, clock=clock)
    pacer.push("x" * 900)
    pacer.finish()
    out = _drain(pacer, clock)
    assert out == "x" * 900
    # 900 chars at 60 cps would take 15 s; the finished drain must be much
    # faster (bounded by the DRAIN_S deadline plus the MAX_RATE ceiling).
    assert clock.t - 100.0 < 4.0


def test_a_long_gui_stall_cannot_dump_more_than_the_burst_cap():
    clock = FakeClock()
    pacer = CharPacer(cps=100, clock=clock)
    pacer.push("a" * 5000)
    clock.advance(10.0)  # window frozen for ten seconds
    chunk = pacer.tick()
    assert 0 < len(chunk) <= CharPacer.MAX_BURST
    assert pacer.pending > 0


def test_punctuation_does_not_pause_the_reveal():
    clock = FakeClock()
    pacer = CharPacer(cps=100, clock=clock)
    pacer.push("Wait. Now!")
    pacer.finish()
    seen = _drain(pacer, clock)
    assert seen == "Wait. Now!"
    assert pacer.done


def test_reveal_has_no_voice_api():
    """The audio-synchronised surface is gone, not merely unused."""
    for name in ("audio_started", "audio_ended", "advance_audio", "set_clock"):
        assert not hasattr(TypewriterReveal, name), name
        assert not hasattr(CharPacer, name), name


def test_cps_is_clamped_to_a_readable_range():
    pacer = CharPacer(cps=1)
    assert pacer.cps == CharPacer.MIN_CPS
    pacer = CharPacer(cps=9999)
    assert pacer.cps == CharPacer.MAX_CPS


# ── the reveal starts immediately, at a steady rate, and never stalls ───────

def test_first_character_appears_on_the_first_tick():
    """No pre-buffer hold: the first letter is visible on the first tick with
    any elapsed time at all, and a zero-time tick cannot reveal anything."""
    clock = FakeClock()
    pacer = CharPacer(cps=60, clock=clock)
    pacer.push("Hello")
    assert pacer.tick() == "", "a tick with no elapsed time reveals nothing"
    clock.advance(0.008)                  # one 8 ms GUI tick
    assert pacer.tick() == "H", "the first character must not wait for a rate"


def test_steady_rate_with_no_taper():
    """An unfinished reply reveals at exactly the configured rate: no
    arrival-aware slowdown as the local backlog shrinks."""
    clock = FakeClock()
    pacer = CharPacer(cps=110, clock=clock)
    pacer.push("word " * 60)              # 300 characters queued
    emitted = 0
    for _ in range(31):                   # 0.496 s of 16 ms ticks
        clock.advance(0.016)
        emitted += len(pacer.tick())
    assert 50 <= emitted <= 60, (
        f"expected ~55 characters in 0.5 s at 110 cps, got {emitted}")


def test_three_fragments_reveal_continuously_without_a_stall():
    """Text arriving in fragments with gaps must not stop-then-proceed: every
    0.4 s window reveals the steady amount, including the very first one."""
    clock = FakeClock()
    pacer = CharPacer(cps=110, clock=clock)
    fragment = "abcdefghij" * 6           # 60 characters per fragment
    windows = []
    for _ in range(3):
        pacer.push(fragment)
        emitted = 0
        for _ in range(25):               # a 0.4 s pause in the model stream
            clock.advance(0.016)
            emitted += len(pacer.tick())
        windows.append(emitted)
    assert all(40 <= chars <= 50 for chars in windows), windows
    assert pacer.pending > 0, "the fragments should keep the reveal fed"


def test_finished_reply_tail_arrives_within_a_short_deadline():
    """`finish()` is a deadline, not just a faster constant: the last 100
    characters of a finished reply land well inside a third of a second."""
    clock = FakeClock()
    pacer = CharPacer(cps=60, clock=clock)
    pacer.push("x" * 900)
    pacer.finish()
    emitted = 0
    tail_start = None
    for _ in range(5000):
        clock.advance(0.016)
        emitted += len(pacer.tick())
        if tail_start is None and pacer.pending <= 100:
            tail_start = clock.t
        if pacer.done:
            break
    assert emitted == 900
    assert tail_start is not None
    assert clock.t - tail_start < 0.3, (
        f"the final 100 characters took {clock.t - tail_start:.3f}s")


# ── the reveal layer: lossless delivery ─────────────────────────────────────

def test_a_failed_emit_never_loses_revealed_characters():
    """A paint error must not drop text: the chunk goes back to the front of
    the queue and the reveal keeps going."""
    clock = FakeClock()
    delivered: list[str] = []
    attempts = {"n": 0}

    def emit(chunk: str) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("paint failed")
        delivered.append(chunk)

    reveal = TypewriterReveal(emit, cps=110, clock=clock)
    text = "Every character survives a paint error."
    reveal.push(text)
    reveal.finish()
    for _ in range(2000):
        clock.advance(0.016)
        reveal._on_tick()
        if reveal._pacer.done:
            break
    assert "".join(delivered) == text
    assert attempts["n"] > 1, "the failed chunk must have been retried"


def test_drain_now_reveals_text_without_finishing_the_reply():
    """The backend needs the first fragment readable at once, but the turn is
    not over: `drain_now` must not fire `on_done` or finish the reveal."""
    clock = FakeClock()
    delivered: list[str] = []
    done: list[bool] = []
    reveal = TypewriterReveal(delivered.append, cps=110, clock=clock)
    reveal.set_on_done(lambda: done.append(True))

    reveal.push("Readable at once.")
    reveal.drain_now()

    assert "".join(delivered) == "Readable at once."
    assert done == [], "drain_now must not finish the turn"
    assert not reveal._pacer.done
    # The same reply may still take more text afterwards.
    clock.advance(0.5)
    reveal.push(" More.")
    for _ in range(400):
        clock.advance(0.016)
        reveal._on_tick()
        if reveal._pacer.idle:
            break
    assert "".join(delivered) == "Readable at once. More."
    assert done == []


# ── grapheme clusters stay atomic ───────────────────────────────────────────

def test_zwj_and_emoji_sequences_are_never_split():
    clock = FakeClock()
    pacer = CharPacer(cps=110, clock=clock)
    family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"      # 👨‍👩‍👧
    thumb = "\U0001F44D\U0001F3FD"                              # 👍🏽
    text = f"A{family}{thumb}e\u0301 tail that keeps the queue busy"
    pacer.push(text)
    pacer.finish()
    chunks: list[str] = []
    for _ in range(500):
        clock.advance(0.016)
        chunk = pacer.tick()
        if chunk:
            chunks.append(chunk)
        if pacer.done:
            break
    assert "".join(chunks) == text
    for chunk in chunks:
        assert not chunk.endswith("\u200d"), f"split ZWJ sequence: {chunk!r}"
        first = chunk[0]
        assert not (first in "\u200d\ufe0e\ufe0f"
                    or "\U0001F3FB" <= first <= "\U0001F3FF"), (
            f"chunk starts with an attaching character: {chunk!r}")
    assert any(family in chunk for chunk in chunks), "family emoji was split"
    assert any(thumb in chunk for chunk in chunks), "skin-tone emoji was split"