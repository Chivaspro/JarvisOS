"""Live speech sink: speak the reply WHILE the model is still producing it.

The gap this closes
-------------------
Every renderer used to wait for a finished sentence before it could speak, and
``local`` voice mode (where the cloud engines live - Fish Audio above all)
waited for the whole reply: the text arrived as fragments, accumulated in a
buffer, and only at turn end was it handed to TTS. A three-sentence answer
therefore paid: full generation time + first-request time-to-first-byte.

This module owns the missing piece: one bounded intake that accepts reply text
AS IT ARRIVES, cuts it at punctuation (never mid-word, never waiting for a
final period), and speaks it through whichever renderer is configured:

* ``fish`` backend - the Fish Audio realtime WebSocket session
  (``voice/tts.py``). Incremental TEXT goes in, audio chunks come out, and
  playback starts on the first chunk. This is the renderer that makes the
  difference audible, because a per-sentence HTTP request is replaced by one
  session for the whole turn.
* ``engine`` backend - every other engine (Edge, SAPI, Kokoro, ElevenLabs,
  Piper/ONNX). Chunks go through the existing :class:`voice.tts.TTSPlayer`;
  the bounded chunk queue and the single worker thread are what keep this
  ordered and off the Qt thread.

Contract (mirrors ``voice/hybrid_voice.py::PiperSpeechService`` so the two
pipelines behave the same to their callers):

    feed(text)   -> queued chunks   (delta OR cumulative transcript, safe either way)
    flush()      -> queued chunks   (end of turn: speak the tail, close the session)
    interrupt()  -> cleared chunks  (stop now; nothing from this turn resumes)
    speak(text)  -> queued chunks   (a whole line, e.g. a corrected reply)

Design rules:

* One bounded queue, one long-lived worker (per turn for the WebSocket, since
  the session is per turn). A full queue DROPS rather than blocking the caller:
  the Gemini receive loop must never stall because a voice fell behind.
* An epoch + cancel pair, bumped on ``interrupt``, so an interrupted reply can
  never resume - not from the queue, not from inside the engine.
* Nothing here touches Qt. State is published through ``on_state`` callbacks
  (main.py maps them to the HUD), and synthesis never runs on the GUI thread.
* Every public method is guarded: a broken engine must cost the voice, never
  the conversation.
"""
from __future__ import annotations

import queue
import threading
import time

__all__ = ["StreamingSpeechService", "speech_backend_for", "FISH_ENGINES"]

_PHASES = ("idle", "buffering", "synthesizing", "playing", "error")

#: Engine names (voice/tts.py::create_tts_player) that can render incrementally
#: over the Fish realtime WebSocket.
FISH_ENGINES = frozenset({"fish_audio", "fish", "fishaudio"})

_END = object()          # end-of-turn sentinel for the text queue


def _realtime_enabled(explicit=None) -> bool:
    """Is genuine realtime speech on? Settings ▸ Automation ▸ Streaming TTS.

    ``explicit`` lets a caller (or a test) override the stored feature. A
    missing settings layer is not a reason to fall back to the slow path.
    """
    if explicit is not None:
        return bool(explicit)
    try:
        from config.settings import get_feature
        return bool(get_feature("tts_realtime", get_feature("tts_streaming", True)))
    except Exception:
        return True


def speech_backend_for(player, cfg=None) -> str:
    """``"fish"`` when the Fish realtime session can render this configuration.

    Decided from the player's ENGINE NAME, which ``create_tts_player`` knows
    before the engine object is built - asking the engine itself would force a
    heavy model (Piper/ONNX) to load just to answer this question.
    """
    try:
        name = str(getattr(player, "_name", "") or "").strip().lower()
    except Exception:
        return "engine"
    return "fish" if name in FISH_ENGINES else "engine"


class StreamingSpeechService:
    """One process-wide live speech sink. Thread-safe; never raises."""

    def __init__(self, cfg=None, *, player=None, session_factory=None,
                 max_queue: int = 12, realtime=None,
                 player_factory=None, state_sink=None):
        self._cfg = dict(cfg or {})
        self.max_queue = max(1, int(max_queue or 12))
        self._player = player
        self._player_factory = player_factory
        self._session_factory = session_factory
        self._realtime = realtime

        self._lock = threading.RLock()
        self._queue: "queue.Queue" = queue.Queue(maxsize=self.max_queue)
        self._text_queue: "queue.Queue" = queue.Queue(maxsize=self.max_queue)
        self._worker: threading.Thread | None = None
        self._session_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._epoch = 0
        self._fed = ""            # reply text received this turn (all of it)
        self._pending = ""        # text with no boundary yet
        self._chars_sent = 0      # characters actually handed to a renderer
        self._idle_deadline = None   # set by prepare(): close an unused session
        self._session_consumed: list[str] = []   # text a live session has taken
        self._session_audio = False              # any audio heard this session?
        self._phase = "idle"
        self._inflight = 0       # chunks accepted but not yet rendered
        self._dropped = 0
        self._spoken = 0
        self._last_reason = ""
        self._backend = ""
        self._first_audio_marked = False
        self._on_state: list = []
        self._state_sink = state_sink

    # ── state ────────────────────────────────────────────────────────────────

    def on_state(self, cb) -> None:
        """Register ``cb(phase)``; called off the GUI thread."""
        with self._lock:
            if cb not in self._on_state:
                self._on_state.append(cb)

    def _set_phase(self, phase: str) -> None:
        phase = str(phase or "idle")
        with self._lock:
            if phase == self._phase:
                return
            self._phase = phase
            callbacks = list(self._on_state)
        for cb in callbacks:
            try:
                cb(phase)
            except Exception:
                pass
        sink = self._state_sink
        if sink is not None:
            try:
                sink(phase)
            except Exception:
                pass

    def phase(self) -> str:
        return self._phase

    def is_speaking(self) -> bool:
        """True while sound is coming out or is about to. Never raises."""
        try:
            with self._lock:
                phase = self._phase
                session = self._session_thread
                inflight = self._inflight
            if phase in ("synthesizing", "playing"):
                return True
            if session is not None and session.is_alive():
                return True
            # Chunks accepted and not yet rendered. Deliberately a COUNTER, not
            # "the queue is not empty": the shutdown sentinel is not speech.
            return inflight > 0
        except Exception:
            return False

    def status(self) -> dict:
        """Diagnostics for the HUD/settings: never raises, never leaks text."""
        try:
            return {
                "phase": self._phase,
                "speaking": self.is_speaking(),
                "backend": self._backend,
                "queued": self._queue.qsize(),
                "pending_text": self._text_queue.qsize(),
                "max_queue": self.max_queue,
                "dropped": self._dropped,
                "spoken": self._spoken,
                "chars_sent": self._chars_sent,
                "reason": self._last_reason,
            }
        except Exception:
            return {"phase": self._phase, "speaking": False}

    # ── intake ───────────────────────────────────────────────────────────────

    def feed(self, text) -> int:
        """Accept a streaming reply fragment (delta OR cumulative). Returns chunks queued.

        Only the NEW suffix is used: Gemini's output transcription arrives as
        deltas in some SDK versions and as a growing cumulative string in
        others. This makes both safe - nothing is ever spoken twice.
        """
        try:
            fragment = str(text or "")
            if not fragment:
                return 0
            from voice.hybrid_voice import split_speech_chunks
            with self._lock:
                if self._fed and fragment.startswith(self._fed):
                    new = fragment[len(self._fed):]
                else:
                    new = fragment
                self._fed += new
                self._pending += new
                chunks, self._pending = split_speech_chunks(self._pending)
                self._chars_sent += sum(len(c) for c in chunks)
            return self._submit(chunks)
        except Exception as exc:
            self._last_reason = str(exc)
            return 0

    def warmup(self) -> bool:
        """Build the voice ENGINE now so the first turn never pays for it.

        Called once from the background prewarm at startup for local voice
        modes. Deliberately does NOT open the realtime session: a session with
        no turn behind it would sit idle and be closed anyway (and a socket
        held open for nothing is a socket billed for nothing). The session is
        opened at the start of the turn instead - see :meth:`prepare`.
        Returns True when the engine is ready.
        """
        try:
            player = self._ensure_player()
            if player is None:
                return False
            hook = getattr(player, "warmup", None)
            if callable(hook):
                hook()
            return True
        except Exception as exc:
            self._last_reason = str(exc)
            return False

    def prepare(self, timeout: float = 6.0) -> bool:
        """Open the realtime session NOW, before the first text exists.

        The WebSocket handshake was measured at ~0.6-0.9 s against the real
        account, and it sits on the critical path when the session is opened on
        the first text chunk. Starting it at the beginning of the turn hides
        that handshake behind the model's own first-token latency instead of
        adding it to the first phrase.

        Purely optional and bounded: with no text within ``timeout`` seconds
        the prepared session closes itself and the next ``feed`` opens a fresh
        one, so a tool-only turn never leaves a socket (or a charge) behind.
        Returns True when a session was actually opened.
        """
        try:
            if not self._use_fish():
                return False
            with self._lock:
                alive = (self._session_thread is not None
                         and self._session_thread.is_alive())
                if alive:
                    return True
                self._idle_deadline = time.monotonic() + max(1.0, float(timeout))
            self._ensure_session()
            return True
        except Exception as exc:
            self._last_reason = str(exc)
            return False

    def speak(self, text) -> int:
        """Submit a whole line (corrected replies, tests): tail included."""
        try:
            from voice.hybrid_voice import split_speech_chunks
            chunks, rest = split_speech_chunks(str(text or ""))
            if rest:
                chunks.append(rest)
            with self._lock:
                self._chars_sent += sum(len(c) for c in chunks)
            return self._submit(chunks)
        except Exception as exc:
            self._last_reason = str(exc)
            return 0

    def flush(self) -> int:
        """End of turn: speak the buffered remainder and close the session."""
        try:
            with self._lock:
                tail, self._pending = self._pending.strip(), ""
                self._fed = ""
            queued = self._submit([tail] if tail else [])
            with self._lock:
                session_live = (self._session_thread is not None
                                and self._session_thread.is_alive())
                backend = self._backend
            if session_live and backend == "fish":
                # Only a session that is actually running needs the end-of-text
                # signal; opening a WebSocket just to close it is pure latency.
                self._submit_text([], end=True)
            return queued
        except Exception as exc:
            self._last_reason = str(exc)
            return 0

    # ── backend plumbing ─────────────────────────────────────────────────────

    def _ensure_player(self):
        with self._lock:
            if self._player is not None:
                return self._player
        factory = self._player_factory
        if factory is None:
            try:
                from voice.tts import create_tts_player
                factory = create_tts_player
            except Exception as exc:
                self._last_reason = str(exc)
                return None
        try:
            player = factory(dict(self._cfg) or _read_config())
        except Exception as exc:
            self._last_reason = str(exc)
            self._set_phase("error")
            return None
        with self._lock:
            if self._player is None:
                self._player = player
            return self._player

    def _use_fish(self) -> bool:
        """Is this turn rendered by the Fish realtime WebSocket?"""
        try:
            if not _realtime_enabled(self._realtime):
                return False
            with self._lock:
                cached = self._backend
            if cached:
                return cached == "fish"
            player = self._ensure_player()
            if player is None:
                return False
            if speech_backend_for(player, self._cfg) != "fish":
                with self._lock:
                    self._backend = "engine"
                return False
            session_factory = self._session_factory
            if session_factory is None:
                session_factory = self._default_session_factory()
                if session_factory is None:
                    with self._lock:
                        self._backend = "engine"
                        self._last_reason = ("Fish realtime session unavailable — "
                                             "speaking through the engine path")
                    return False
                # Pre-flight the real factory: it needs a real Fish engine, and
                # "the player is named fish_audio" is not the same thing as "the
                # engine can be built". Building a FISH engine is trivial (no
                # model to load), so this costs nothing and avoids opening a
                # socket that can only fail.
                try:
                    _engine = player._ensure_engine()
                    from voice.tts import FishAudioTTSEngine
                    if not isinstance(_engine, FishAudioTTSEngine):
                        with self._lock:
                            self._backend = "engine"
                            self._last_reason = ("the configured voice is not a "
                                                 "Fish Audio engine")
                        return False
                except Exception as exc:
                    with self._lock:
                        self._backend = "engine"
                        self._last_reason = f"Fish Audio engine unavailable: {exc}"
                    return False
                with self._lock:
                    self._session_factory = session_factory
            with self._lock:
                self._backend = "fish"
            return True
        except Exception as exc:
            self._last_reason = str(exc)
            return False

    def _default_session_factory(self):
        """The real Fish WebSocket session, when this build has one."""
        try:
            from voice.tts import fish_stream_session
        except Exception:
            return None
        return fish_stream_session if callable(fish_stream_session) else None

    def _submit(self, chunks) -> int:
        chunks = [c for c in (chunks or []) if c]
        if not chunks:
            return 0
        if self._use_fish():
            return self._submit_text(chunks)
        return self._enqueue_chunks(chunks)

    # ── engine backend (chunk queue + one worker) ────────────────────────────

    def _enqueue_chunks(self, chunks) -> int:
        if not chunks:
            return 0
        self._ensure_worker()
        queued = 0
        for chunk in chunks:
            with self._lock:
                epoch = self._epoch
            try:
                self._queue.put_nowait((epoch, chunk))
                with self._lock:
                    self._inflight += 1
                queued += 1
            except queue.Full:
                # Bounded on purpose: never stall the Live receive loop and never
                # grow a backlog of stale speech.
                self._dropped += 1
        return queued

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run_worker,
                                            name="jarvis-speech-stream", daemon=True)
            self._worker.start()

    def _run_worker(self) -> None:
        while True:
            try:
                item = self._queue.get()
            except Exception:
                return
            try:
                if item is None:
                    return
                epoch, text = item
                with self._lock:
                    stale = epoch != self._epoch
                    cancel = self._cancel
                    self._inflight = max(0, self._inflight - 1)
                if stale or cancel.is_set():
                    continue
                player = self._ensure_player()
                if player is None:
                    continue
                self._set_phase("synthesizing")
                try:
                    player.speak(text, cancel=cancel,
                                 on_audio=self._on_audio_started)
                    self._spoken += 1
                except Exception as exc:
                    self._last_reason = str(exc)
                    self._set_phase("error")
            except Exception as exc:
                self._last_reason = str(exc)
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass
                if self._queue.empty() and self._phase != "error":
                    self._set_phase("idle")

    def _on_audio_started(self) -> None:
        with self._lock:
            first = not self._first_audio_marked
            self._first_audio_marked = True
        if first:
            self._mark("playback")
        self._set_phase("playing")

    @staticmethod
    def _mark(tag: str) -> None:
        """Feed the turn timeline (never fatal, never required)."""
        try:
            from voice import turn_timeline
            turn_timeline.mark(tag)
        except Exception:
            pass

    # ── fish backend (one WebSocket session per turn) ────────────────────────

    def _submit_text(self, chunks, end: bool = False) -> int:
        """Push TEXT chunks to the running WebSocket session, starting it if needed."""
        queued = 0
        if chunks:
            self._ensure_session()
            for chunk in list(chunks):
                try:
                    self._text_queue.put_nowait(chunk)
                    queued += 1
                except queue.Full:
                    self._dropped += 1
        if end:
            try:
                self._text_queue.put_nowait(_END)
            except queue.Full:
                # The session must still learn the turn is over: drop the oldest
                # chunk (already stale audio by definition of being late).
                try:
                    self._text_queue.get_nowait()
                    self._text_queue.put_nowait(_END)
                except Exception:
                    pass
        return queued

    def _ensure_session(self) -> None:
        with self._lock:
            if self._session_thread is not None and self._session_thread.is_alive():
                return
            epoch = self._epoch
            cancel = self._cancel
            factory = self._session_factory
            player = self._player
            self._session_consumed = []
            self._session_audio = False
            # The idle deadline belongs to THIS session: it is read here and
            # cleared, so a session that timed out can never leave an expired
            # deadline behind to kill the next one instantly.
            idle_deadline = self._idle_deadline
            self._idle_deadline = None
            self._session_thread = threading.Thread(
                target=self._run_session,
                args=(factory, epoch, cancel, player, idle_deadline),
                name="jarvis-fish-stream", daemon=True)
            self._session_thread.start()

    def _session_audio_started(self) -> None:
        """Audio really arrived on this session (so it is not a silent failure)."""
        with self._lock:
            self._session_audio = True
        self._on_audio_started()

    def _run_session(self, factory, epoch, cancel, player,
                     idle_deadline=None) -> None:
        self._set_phase("synthesizing")
        failed: Exception | None = None
        try:
            factory(self._text_source(epoch, cancel, idle_deadline), cancel=cancel,
                    cfg=dict(self._cfg), player=player,
                    on_audio=self._session_audio_started)
            self._spoken += 1
        except Exception as exc:
            failed = exc
        with self._lock:
            stale = epoch != self._epoch
        if failed is not None and not stale and not cancel.is_set():
            # The session died mid-turn. Whatever was already HEARD is never
            # repeated (that would double-speak); text the session took but
            # never voiced is re-rendered through the engine path, so a broken
            # socket costs latency, not the reply. Everything after this turn
            # goes through the engine path too - no dead socket is reused.
            self._last_reason = str(failed)
            with self._lock:
                self._backend = "engine"
            self._note("voice_error", failed)
            self._requeue_after_session_failure()
            self._set_phase("error")
        elif not stale:
            self._set_phase("idle")

    def _requeue_after_session_failure(self) -> None:
        """Speak what the failed session never voiced, through the engine path."""
        try:
            with self._lock:
                heard = self._session_audio
                lost = list(self._session_consumed)
                self._session_consumed = []
            if heard:
                return                      # partially heard: never replay
            rest: list[str] = []
            while True:
                try:
                    item = self._text_queue.get_nowait()
                except Exception:
                    break
                try:
                    if item is _END:
                        continue
                    rest.append(str(item))
                except Exception:
                    continue
            chunks = [c for c in (" ".join(lost + rest)).split() if c]
            if chunks:
                self._enqueue_chunks([" ".join(chunks)])
        except Exception as exc:
            self._last_reason = str(exc)

    @staticmethod
    def _note(key: str, value) -> None:
        """Feed the turn timeline's notes (never fatal, never required)."""
        try:
            from voice import turn_timeline
            turn_timeline.note(key, value)
        except Exception:
            pass

    def _text_source(self, epoch: int, cancel: threading.Event,
                     idle_deadline: float | None = None):
        """Generator handed to the WebSocket sender: text until END or cancel.

        ``idle_deadline`` (only set by ``prepare()``) closes a session that was
        opened early for a turn which never produced text, instead of holding a
        socket open for nothing.
        """
        while True:
            if cancel.is_set():
                return
            with self._lock:
                if epoch != self._epoch:
                    return
            if idle_deadline is not None and time.monotonic() > idle_deadline:
                return
            try:
                item = self._text_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            except Exception:
                return
            try:
                if item is _END:
                    return
                with self._lock:
                    self._session_consumed.append(str(item))
                self._mark("tts_submit")
                yield str(item)
            finally:
                try:
                    self._text_queue.task_done()
                except Exception:
                    pass

    # ── interruption ─────────────────────────────────────────────────────────

    def interrupt(self, log: bool = True) -> int:
        """Stop NOW: cancel the renderer, clear both queues, reset the turn.

        Everything from the interrupted reply is discarded and an epoch bump
        guarantees none of it can resume - not a queued sentence, not text the
        WebSocket sender still holds.
        """
        with self._lock:
            self._epoch += 1
            self._fed = ""
            self._pending = ""
            self._first_audio_marked = False
            self._inflight = 0
            self._idle_deadline = None
            self._session_consumed = []
            self._session_audio = False
            old_cancel = self._cancel
            self._cancel = threading.Event()
            cleared = 0
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    cleared += 1
                except Exception:
                    break
            while True:
                try:
                    self._text_queue.get_nowait()
                    self._text_queue.task_done()
                    cleared += 1
                except Exception:
                    break
        old_cancel.set()
        self._stop_playback()
        self._set_phase("idle")
        if log:
            print(f"[SPEECH] interrupted — playback stopped, "
                  f"{cleared} queued item(s) cleared")
        return cleared

    @staticmethod
    def _stop_playback() -> None:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Process shutdown: silence, drain and join the workers (bounded)."""
        try:
            self.interrupt(log=False)
        except Exception:
            pass
        for thread in (self._session_thread, self._worker):
            if thread is not None and thread.is_alive():
                try:
                    thread.join(timeout=2.0)
                except Exception:
                    pass
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                try:
                    self._queue.put_nowait(None)
                except Exception:
                    pass


def _read_config() -> dict:
    try:
        from core.brain_bridge import api_config  # type: ignore  # may not exist
        cfg = api_config()
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    try:
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "config" / "api_keys.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── process-wide instance ────────────────────────────────────────────────────

_INSTANCE: StreamingSpeechService | None = None
_INSTANCE_LOCK = threading.Lock()
_INSTANCE_KEY: tuple = ()


def get_speech_stream(cfg: dict | None = None, **kwargs) -> StreamingSpeechService:
    """The process-wide sink, rebuilt only when the voice configuration changes.

    Keyed like the TTS engine cache so a settings save (engine/voice/key change)
    takes effect on the next turn without a restart, while an unchanged
    configuration reuses the live WebSocket-capable service and its engine.
    """
    global _INSTANCE, _INSTANCE_KEY
    key = _instance_key(cfg)
    with _INSTANCE_LOCK:
        if _INSTANCE is not None and key == _INSTANCE_KEY:
            if cfg is not None:
                _INSTANCE._cfg = dict(cfg)
            return _INSTANCE
        old = _INSTANCE
        _INSTANCE = StreamingSpeechService(cfg, **kwargs)
        _INSTANCE_KEY = key
    if old is not None:
        try:
            old.shutdown()
        except Exception:
            pass
    return _INSTANCE


def _instance_key(cfg: dict | None) -> tuple:
    cfg = cfg or {}
    try:
        from voice.providers_config import active_provider, providers_block
        provider = active_provider(cfg)
        subtree = providers_block(cfg)["providers"].get(provider, {})
        return (provider, str(sorted((k, str(v)) for k, v in subtree.items())))
    except Exception:
        return (str(cfg.get("tts_engine") or ""),)
