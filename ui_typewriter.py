"""Time-based, letter-by-letter typewriter reveal, independent of the voice.

The chat reveals the reply at a steady, configurable character rate
(``typewriter_cps``, default 110). Nothing here observes TTS, the playback
clock or any audio state: muted, silent, text-only and spoken turns all
animate identically.

Design rules
------------
* One character at a time, right from the first fragment. A tick with any
  elapsed time reveals at least one character, so the opening words are
  readable the moment the backend flushes them (there is no pre-buffer hold
  and no "wait until enough text arrived" stall).
* Constant speed from the first token. Output is driven by real elapsed time,
  so the average rate is constant no matter how the model splits its text
  (word, sentence or paragraph chunks make no difference) and no matter how
  uneven the GUI timer is. No punctuation pauses, no arrival-aware taper and
  no per-chunk speed changes.
* Continuous, never a mid-message stop. The speed only ever rises above the
  configured ``cps``, and only when a long backlog would otherwise take more
  than ``BACKLOG_S`` seconds. That catch-up works toward a DEADLINE
  (``pending / remaining_time``) rather than a proportional
  ``pending / constant`` rate, so it accelerates smoothly and actually
  converges on the goal instead of creeping.
* No catch-up dump. A frozen GUI thread is charged at most ``MAX_DT`` per
  tick, any armed deadline slides by the time the cap swallowed (a frozen
  window is not repaid), and one tick can never release more than
  ``MAX_BURST`` characters.
* Finished replies drain promptly: ``finish()`` arms a ``DRAIN_S`` deadline,
  so whatever is still hidden arrives in a fraction of a second.
* Exact text. The concatenation of every emitted chunk equals the pushed
  text. Combining marks, variation selectors and ZWJ sequences are released
  together with their base character so emoji / accents never flicker.
* Flush is lossless and idempotent, and a paint failure never drops revealed
  characters: the reveal re-queues the chunk it could not deliver.

``CharPacer`` is pure Python with an injectable clock (unit-testable).
``TypewriterReveal`` is the QTimer wrapper used by the chat window.
"""

from __future__ import annotations

import os
import time
import unicodedata
from collections import deque
from typing import Callable, Deque, Optional

try:                                   # keep the pacer importable without Qt
    from PyQt6.QtCore import QObject, QTimer, Qt
except Exception:                      # pragma: no cover
    QObject = object                   # type: ignore
    QTimer = None                      # type: ignore
    Qt = None                          # type: ignore


_TW_DEBUG = bool(os.environ.get("JARVIS_TW_DEBUG"))


def _attaches_to_previous(ch: str) -> bool:
    """True for characters that must appear together with the one before."""
    if not ch:
        return False
    return (ch in "\u200d\ufe0e\ufe0f"
            or unicodedata.combining(ch) != 0
            or "\U0001F3FB" <= ch <= "\U0001F3FF")      # skin-tone modifiers


class CharPacer:
    """Decide how many characters are visible now. No Qt, no audio, no I/O."""

    #: Readable range for the configured reveal speed (``typewriter_cps``).
    MIN_CPS, MAX_CPS = 20.0, 320.0
    #: A stalled GUI thread must not dump a paragraph: the elapsed time charged
    #: to one tick is capped here, in seconds.
    MAX_DT = 0.10
    #: Hard ceiling for any catch-up rate, in characters/second. Even an
    #: overdue deadline cannot push the reveal past this.
    MAX_RATE = 600.0
    #: Hard ceiling for the characters released by a single tick. This is what
    #: keeps one long GUI stall from becoming a wall of text; a grapheme
    #: cluster released with its last character may add a few code points.
    MAX_BURST = 64
    #: ``finish()`` promises everything still hidden arrives within this many
    #: seconds: the tail is paced to hit that deadline, never dropped.
    DRAIN_S = 0.20
    #: While text is still arriving the reveal never falls further behind than
    #: this: a backlog longer than ``cps * BACKLOG_S`` characters arms a
    #: deadline that the rate then works toward. Small, normal fragments get
    #: no deadline at all and are revealed at exactly ``cps``.
    BACKLOG_S = 8.0

    def __init__(self, cps: float = 110.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.cps = max(self.MIN_CPS, min(self.MAX_CPS, float(cps)))
        self.reset()

    def reset(self) -> None:
        self._pending: Deque[str] = deque()
        self._budget = 0.0
        self._last: Optional[float] = None
        #: Absolute clock time by which the queue should be empty, or ``None``
        #: when the reveal is running at its normal ``cps``.
        self._deadline: Optional[float] = None
        #: True once this burst of text has revealed its first character.
        self._primed = False
        self._finished = False
        self.emitted = 0

    # -- input -----------------------------------------------------------
    def push(self, text: str) -> None:
        """Queue more text. Never held back, never changes the speed."""
        text = str(text or "")
        if not text:
            return
        if not self._pending:
            # Starting, or resuming after the queue ran dry: restart the clock
            # and the character budget. Time spent waiting for the model is
            # never "paid back" as a burst. A finished reply that is being
            # extended restarts as a live stream again.
            self._last = self._clock()
            self._budget = 0.0
            self._primed = False
            self._deadline = None
        self._pending.extend(text)
        if self._deadline is None and len(self._pending) > self.cps * self.BACKLOG_S:
            # Only a genuinely long backlog gets a deadline. The rate then
            # works toward it (see ``_current_rate``) instead of creeping at a
            # proportional ``pending / constant`` speed.
            self._deadline = self._clock() + self.BACKLOG_S

    def requeue(self, text: str) -> None:
        """Put characters back at the FRONT of the queue (a paint failed).

        Order is preserved, so the failed emission is retried before any newer
        text and the concatenation of every emitted chunk still equals the
        pushed text.
        """
        text = str(text or "")
        if not text:
            return
        self._pending.extendleft(reversed(text))
        if self._last is None:
            self._last = self._clock()

    def finish(self) -> None:
        """No more text is coming for this reply: drain to a short deadline."""
        self._finished = True
        if self._pending:
            self._deadline = self._clock() + self.DRAIN_S

    # -- state -----------------------------------------------------------
    @property
    def idle(self) -> bool:
        return not self._pending

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def done(self) -> bool:
        return self._finished and not self._pending

    @property
    def pending(self) -> int:
        return len(self._pending)

    # -- output ----------------------------------------------------------
    def _current_rate(self, now: float) -> float:
        """Characters per second to reveal now.

        At the configured ``cps`` unless a deadline is armed; then the rate is
        the DEADLINE rate (``pending / remaining_time``), never a proportional
        ``pending / constant`` creep, so the backlog is guaranteed to clear.
        """
        rate = self.cps
        if self._deadline is not None:
            remaining = max(0.008, self._deadline - now)
            rate = max(rate, len(self._pending) / remaining)
        return min(self.MAX_RATE, rate)

    def tick(self) -> str:
        """Characters that became visible since the last tick."""
        now = self._clock()
        last, self._last = self._last, now
        if not self._pending:
            self._budget = 0.0
            self._deadline = None
            return ""
        if last is None:
            return ""
        raw = max(0.0, now - last)
        dt = min(self.MAX_DT, raw)
        swallowed = raw - dt
        if swallowed > 0.0 and self._deadline is not None:
            # A frozen GUI is not repaid: the deadline slides forward by the
            # time the cap refused to charge, so the drain window never
            # collapses into one catch-up dump when the loop unfreezes.
            self._deadline += swallowed
        if dt <= 0.0:
            return ""                  # no elapsed time, nothing can reveal
        self._budget += dt * self._current_rate(now)
        n = int(self._budget)
        if n <= 0 and not self._primed:
            # Letter-by-letter reveal must START on the first tick that has any
            # elapsed time: one character now, the rest at the steady rate.
            n = 1
        self._primed = True
        n = min(n, self.MAX_BURST, len(self._pending))
        if n <= 0:
            return ""
        self._budget -= n

        out: list[str] = []
        for _ in range(n):
            out.append(self._pending.popleft())
        # Never split a character from its combining marks / ZWJ sequence.
        while self._pending and (_attaches_to_previous(self._pending[0])
                                 or out[-1] == "\u200d"):
            out.append(self._pending.popleft())
        self.emitted += len(out)
        if not self._pending:
            self._deadline = None      # the queue is empty: no goal to chase
        return "".join(out)

    def flush(self) -> str:
        """Everything still hidden, immediately. Lossless and idempotent."""
        text = "".join(self._pending)
        self._pending.clear()
        self._budget = 0.0
        self._deadline = None
        self._primed = False
        return text


class TypewriterReveal(QObject):  # type: ignore[misc]
    """Drive a ``CharPacer`` from a QTimer and hand visible text to *emit*.

    All methods must be called on the GUI thread (the chat window already
    marshals everything through ``run_on_ui``).
    """

    TICK_MS = 8     # ~125 Hz: finer than the display, so steps stay even

    def __init__(self, emit: Callable[[str], None], parent=None,
                 cps: float = 110.0,
                 clock: Optional[Callable[[], float]] = None) -> None:
        super().__init__(parent)
        self._emit = emit
        # ``clock`` is injectable so the reveal itself can be unit-tested
        # deterministically (production always uses the monotonic clock).
        self._pacer = CharPacer(cps=cps, clock=clock or time.monotonic)
        self._timer = QTimer(self)
        try:
            # The default coarse timer can fire up to ~5% late and aligns
            # wake-ups, which shows up as uneven letter spacing.
            self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        except Exception:
            pass
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._on_done: Optional[Callable[[], None]] = None
        self._done_fired = False

    # -- configuration ---------------------------------------------------
    def set_emit(self, emit: Callable[[str], None]) -> None:
        self._emit = emit

    def set_cps(self, cps: float) -> None:
        """Apply the configured speed; takes effect from the next turn."""
        self._pacer.cps = max(CharPacer.MIN_CPS,
                              min(CharPacer.MAX_CPS, float(cps)))

    def set_on_done(self, cb: Optional[Callable[[], None]]) -> None:
        self._on_done = cb

    # -- text ------------------------------------------------------------
    def push(self, text: str) -> None:
        self._done_fired = False
        if _TW_DEBUG:
            print(f"[TYPEWRITER] +{len(text)} chars at {time.monotonic():.3f}s "
                  f"(pending before: {self._pacer.pending}) {text[:30]!r}")
        self._pacer.push(text)
        self._ensure_running()

    def finish(self) -> None:
        self._pacer.finish()
        self._ensure_running()

    def drain_now(self) -> None:
        """Reveal everything already pushed, synchronously, without finishing.

        More text may still arrive for this reply, so this intentionally does
        NOT fire ``on_done`` and does not clear the finished state: the normal
        tick still owns the reveal. It exists for the first fragment of a turn,
        which must be readable at the instant the voice starts (the backend
        flushes, then speaks) rather than on a later paint tick.
        """
        try:
            rest = self._pacer.flush()
        except Exception:
            return
        if rest and not self._deliver(rest):
            # A failed paint must not drop text: it is back in the queue.
            self._ensure_running()
            return
        if self._pacer.idle and not self._pacer.finished:
            self._timer.stop()          # nothing waiting right now

    def flush(self) -> None:
        """Show everything now (interrupt, typewriter turned off, next turn)."""
        try:
            rest = self._pacer.flush()
        except Exception:
            rest = ""
        if rest and not self._deliver(rest):
            # A failed paint must not drop text: it is back in the queue and
            # the tick will retry it; do not finish the reveal yet.
            self._ensure_running()
            return
        self._pacer.reset()
        self._timer.stop()
        self._fire_done()

    # -- internals -------------------------------------------------------
    def _ensure_running(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _deliver(self, chunk: str) -> bool:
        """Emit *chunk*. On a paint failure, re-queue it so nothing is lost."""
        try:
            self._emit(chunk)
            return True
        except Exception:
            try:
                self._pacer.requeue(chunk)
            except Exception:
                pass
            return False

    def _on_tick(self) -> None:
        try:
            chunk = self._pacer.tick()
        except Exception:
            self._timer.stop()
            return
        if chunk and not self._deliver(chunk):
            # Never lose revealed characters on a paint error: the chunk is
            # back at the front of the queue, so the next tick retries it.
            self._ensure_running()
            return
        try:
            if self._pacer.done:
                self._timer.stop()
                self._fire_done()
            elif self._pacer.idle and not self._pacer.finished:
                # Waiting for more text from the model; push() restarts it.
                self._timer.stop()
        except Exception:
            self._timer.stop()

    def _fire_done(self) -> None:
        if self._done_fired:
            return
        self._done_fired = True
        cb = self._on_done
        if cb is not None:
            try:
                cb()
            except Exception:
                pass