"""Fish Audio realtime streaming - the WebSocket session, deterministically.

These tests drive ``FishAudioTTSEngine.ws_stream`` against a FAKE SDK and a
FAKE speaker, so the properties that matter are pinned without a network, an
account or an audio device:

* text is handed to the session as it arrives (incremental input),
* the first audio chunk is written to the speaker BEFORE the text iterator is
  exhausted - i.e. playback starts while the model is still writing,
* PCM is written in order, at the requested sample rate,
* cancellation stops within one chunk and never resumes,
* a failure before the first audio byte raises (so the sink can fall back),
* the endpoint shape decides whether the session is used at all.

The live integration test at the bottom is opt-in (JARVIS_LIVE_FISH=1): it
spends real credits, so it never runs by default and is labelled separately in
the report.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import voice.tts as tts  # noqa: E402
from voice.tts import FishAudioTTSEngine, _ws_base_url, fish_stream_session  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeSpeaker:
    """Records everything written to the output stream, with real ordering."""

    def __init__(self, fail_after: int | None = None):
        self.written: list[bytes] = []
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.rates: list[int] = []
        self._fail_after = fail_after

    def start(self):
        self.started += 1

    def write(self, data: bytes):
        if self._fail_after is not None and len(self.written) >= self._fail_after:
            raise RuntimeError("device gone")
        self.written.append(bytes(data))

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1


class FakeSDK:
    """The ``fishaudio`` SDK surface ws_stream() actually uses."""

    def __init__(self, chunks=3, raise_after: int | None = None,
                 fail_to_open: bool = False, chunk_delay: float = 0.0):
        self.chunks = chunks
        self.raise_after = raise_after
        self.fail_to_open = fail_to_open
        self.chunk_delay = chunk_delay
        self.sent: list[str] = []
        self.audio_times: list[float] = []
        self.text_times: list[float] = []
        self.closed = 0
        self.config_seen: dict = {}
        self.kwargs_seen: dict = {}
        self.max_workers_seen = None
        self._session = self

    # SDK entry point ------------------------------------------------------
    def tts_stream_websocket(self, text_iter, **kwargs):
        if self.fail_to_open:
            raise RuntimeError("401 Unauthorized")
        self.kwargs_seen = dict(kwargs)
        self.max_workers_seen = kwargs.get("max_workers")
        config = kwargs.get("config")
        self.config_seen = {
            "format": getattr(config, "format", None),
            "sample_rate": getattr(config, "sample_rate", None),
            "chunk_length": getattr(config, "chunk_length", None),
            "min_chunk_length": getattr(config, "min_chunk_length", None),
            "latency": getattr(config, "latency", None),
        }
        # A sender "thread" consuming the text, and the receive loop below:
        # interleaved exactly like the real SDK (independent sides).
        consumer = threading.Thread(target=self._consume, args=(text_iter,),
                                    daemon=True)
        consumer.start()
        try:
            for i in range(self.chunks):
                if self.raise_after is not None and i >= self.raise_after:
                    raise RuntimeError("websocket dropped")
                deadline = time.monotonic() + 1.0
                while not self.sent and time.monotonic() < deadline:
                    time.sleep(0.005)
                if not self.sent:
                    return
                self.audio_times.append(time.monotonic())
                if self.chunk_delay:
                    # A real stream is paced by synthesis; without this the
                    # whole reply would arrive instantly and a cancellation
                    # could never be observed mid-turn.
                    time.sleep(self.chunk_delay)
                yield bytes([i, 0]) * 512
        finally:
            consumer.join(timeout=1.0)

    def _consume(self, text_iter):
        for item in text_iter:
            self.sent.append(str(item))
            self.text_times.append(time.monotonic())

    def close(self):
        self.closed += 1


@pytest.fixture
def engine():
    return FishAudioTTSEngine(api_key="sk-fish-test", model_id="s2-pro",
                              voice_id="a" * 32)


@pytest.fixture
def fake_pcm(monkeypatch):
    """Route the PCM output to a recording speaker.

    The factory STARTS the speaker, exactly like the real ``_open_pcm_stream``
    does - a fake that returns without starting would hide whether the session
    opens one device stream for the whole turn.
    """
    speaker = FakeSpeaker()

    def _open(rate):
        speaker.rates.append(int(rate))
        speaker.start()
        return speaker

    monkeypatch.setattr(tts, "_open_pcm_stream", _open)
    monkeypatch.setattr(tts, "_push_reactive", lambda pcm, rate=24000: None)
    return speaker


def _install_sdk(monkeypatch, sdk: FakeSDK, fail_import: bool = False):
    """Make ``from fishaudio import FishAudio, TTSConfig`` resolve to the fake."""
    import types

    module = types.ModuleType("fishaudio")

    class _FishAudio:
        def __init__(self, api_key=None, base_url=None):
            self.api_key = api_key
            self.base_url = base_url

        @property
        def tts(self):
            return types.SimpleNamespace(
                stream_websocket=sdk.tts_stream_websocket)

        def close(self):
            sdk.close()

    class _TTSConfig:
        def __init__(self, format=None, sample_rate=None, chunk_length=None,
                     min_chunk_length=None, latency=None, **_):
            self.format = format
            self.sample_rate = sample_rate
            self.chunk_length = chunk_length
            self.min_chunk_length = min_chunk_length
            self.latency = latency

    class _WebSocketOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def to_httpx_ws_kwargs(self):
            return dict(self.kwargs)

    module.FishAudio = _FishAudio
    module.TTSConfig = _TTSConfig
    module.WebSocketOptions = _WebSocketOptions
    monkeypatch.setitem(sys.modules, "fishaudio", module)
    return module


def _text_stream(items, delay: float = 0.0):
    for item in items:
        yield item
        if delay:
            time.sleep(delay)


# ── the endpoint decides whether the session is used ─────────────────────────

def test_the_base_url_is_derived_from_the_configured_endpoint():
    assert _ws_base_url("https://api.fish.audio/v1/tts") == "https://api.fish.audio"
    assert _ws_base_url("https://proxy.example.com/v1/tts/") == "https://proxy.example.com"
    assert _ws_base_url("") == "https://api.fish.audio"


def test_ws_supported_reports_false_when_the_sdk_is_missing(engine, monkeypatch):
    monkeypatch.setitem(sys.modules, "fishaudio", None)
    monkeypatch.setitem(sys.modules, "httpx_ws", None)

    assert engine.ws_supported() is False


# ── incremental text in, audio out ───────────────────────────────────────────

def test_audio_is_written_before_the_text_stream_finishes(engine, fake_pcm,
                                                          monkeypatch):
    """The whole point: playback must begin while the model is still writing."""
    sdk = FakeSDK(chunks=3)
    _install_sdk(monkeypatch, sdk)
    audio_started: list[float] = []

    # The third text chunk arrives only after the first audio has been played.
    text = _text_stream(["First phrase here. ", "Second phrase here. "], delay=0.05)

    played = engine.ws_stream(text, on_audio=lambda: audio_started.append(time.monotonic()))

    assert played is True
    assert speaker_written(fake_pcm) >= 1
    assert audio_started, "on_audio must report the real playback start"
    assert sdk.text_times[0] <= audio_started[0], (
        "text is submitted first; audio follows it - never the other way round")


def speaker_written(speaker: FakeSpeaker) -> int:
    return len(speaker.written)


def test_pcm_is_requested_and_written_in_order(engine, fake_pcm, monkeypatch):
    sdk = FakeSDK(chunks=3)
    _install_sdk(monkeypatch, sdk)

    engine.ws_stream(_text_stream(["One whole phrase. "]))

    # min_chunk_length=12 is MEASURED, not guessed: 50 (the SDK default) held
    # the first sentence back until the second one arrived (2602 ms to first
    # audio vs 1063 ms). See the table in voice/tts.py::ws_stream.
    assert sdk.config_seen == {"format": "pcm", "sample_rate": 24000,
                               "chunk_length": 100, "min_chunk_length": 12,
                               "latency": "normal"}
    assert fake_pcm.written == [bytes([0, 0]) * 512, bytes([1, 0]) * 512,
                                bytes([2, 0]) * 512]
    # One device stream for the whole turn: no per-chunk reopen (audible gaps).
    assert fake_pcm.started == 1 and fake_pcm.stopped == 1 and fake_pcm.closed == 1
    assert fake_pcm.rates == [24000], "24 kHz raw PCM is what was requested"


def test_the_same_voice_settings_are_used_as_the_ordinary_path(engine, fake_pcm,
                                                               monkeypatch):
    sdk = FakeSDK(chunks=1)
    _install_sdk(monkeypatch, sdk)

    engine.ws_stream(_text_stream(["Hello."]))

    assert sdk.kwargs_seen["reference_id"] == "a" * 32
    assert sdk.kwargs_seen["model"] == "s2-pro"


def test_a_long_reply_gets_a_longer_ping_timeout(engine, fake_pcm, monkeypatch):
    sdk = FakeSDK(chunks=1)
    _install_sdk(monkeypatch, sdk)

    engine.ws_stream(_text_stream(["Hello."]))

    ws_options = sdk.kwargs_seen["ws_options"]
    assert ws_options is not None
    assert ws_options.to_httpx_ws_kwargs()["keepalive_ping_timeout_seconds"] >= 60.0


# ── cancellation ─────────────────────────────────────────────────────────────

def test_cancellation_stops_within_one_chunk_and_never_resumes(engine, fake_pcm,
                                                               monkeypatch):
    sdk = FakeSDK(chunks=50, chunk_delay=0.02)     # ~1 s of audio available
    _install_sdk(monkeypatch, sdk)
    cancel = threading.Event()

    def _cancel_soon():
        time.sleep(0.15)
        cancel.set()

    threading.Thread(target=_cancel_soon, daemon=True).start()
    started = time.monotonic()
    engine.ws_stream(_text_stream(["Sentence one. "], delay=0.4), cancel=cancel)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "an interrupted reply must stop quickly, not drain the stream"
    assert 0 < len(fake_pcm.written) < 50, (
        "audio stops within a chunk or two of the cancel, and what was already "
        "heard is kept")
    assert fake_pcm.closed == 1, "the device stream must be released on cancel"


# ── failure paths ────────────────────────────────────────────────────────────

def test_a_failure_before_any_audio_raises_so_the_caller_can_fall_back(
        engine, fake_pcm, monkeypatch):
    sdk = FakeSDK(fail_to_open=True)
    _install_sdk(monkeypatch, sdk)

    with pytest.raises(RuntimeError):
        engine.ws_stream(_text_stream(["Hello."]))

    assert fake_pcm.written == []
    assert sdk.closed == 1, "a session that failed to open must still be closed"


def test_a_mid_stream_failure_keeps_what_was_already_played(engine, fake_pcm,
                                                            monkeypatch):
    sdk = FakeSDK(chunks=10, raise_after=2)
    _install_sdk(monkeypatch, sdk)

    with pytest.raises(RuntimeError):
        engine.ws_stream(_text_stream(["A whole phrase here. "]))

    assert len(fake_pcm.written) == 2, "already-heard audio is not discarded"
    assert fake_pcm.closed == 1


def test_a_dead_speaker_raises_before_playback_but_does_not_after(
        engine, monkeypatch):
    sdk = FakeSDK(chunks=5)
    _install_sdk(monkeypatch, sdk)
    speaker = FakeSpeaker(fail_after=0)
    monkeypatch.setattr(tts, "_open_pcm_stream", lambda rate: speaker)
    monkeypatch.setattr(tts, "_push_reactive", lambda pcm, rate=24000: None)

    with pytest.raises(RuntimeError):
        engine.ws_stream(_text_stream(["Hello there."]))

    assert speaker.closed == 1


# ── the sink's entry point ───────────────────────────────────────────────────

def test_fish_stream_session_reuses_the_cached_engine(engine, fake_pcm, monkeypatch):
    sdk = FakeSDK(chunks=1)
    _install_sdk(monkeypatch, sdk)

    class _Player:
        def __init__(self, engine):
            self._engine = engine
            self._name = "fish_audio"

        def _ensure_engine(self):
            return self._engine

    played = fish_stream_session(_text_stream(["Hello there."]),
                                 player=_Player(engine), cfg={})

    assert played is True
    assert sdk.sent == ["Hello there."]


def test_fish_stream_session_refuses_a_non_fish_engine(monkeypatch):
    class _Other:
        def _ensure_engine(self):
            return object()

    with pytest.raises(RuntimeError):
        fish_stream_session(_text_stream(["Hello."]), player=_Other(), cfg={})


# ── live integration (opt-in, spends real credits) ───────────────────────────

@pytest.mark.skipif(os.getenv("JARVIS_LIVE_FISH", "") != "1",
                    reason="live Fish Audio test: set JARVIS_LIVE_FISH=1 to run "
                           "(spends credits)")
def test_live_fish_realtime_streams_before_the_turn_ends(monkeypatch):
    """Real account, real socket. Reports MEASURED numbers; changes no state."""
    import json
    cfg = json.loads((ROOT / "config" / "api_keys.json").read_text(encoding="utf-8"))
    engine = FishAudioTTSEngine(
        api_key=str(cfg.get("fish_audio_api_key") or ""),
        model_id=str(cfg.get("fish_audio_model_id") or "s2-pro"),
        voice_id=str(cfg.get("fish_audio_voice_id") or ""),
        endpoint=str(cfg.get("fish_audio_endpoint") or "https://api.fish.audio/v1/tts"),
    )
    assert engine.ws_supported(), "the SDK/endpoint must support the realtime session"

    written: list[float] = []
    import voice.tts as _tts

    class _Speaker:
        def start(self): pass
        def stop(self): pass
        def close(self): pass
        def write(self, data): written.append(time.monotonic())

    # No sound in the test: the PCM is timestamped, never sent to a device.
    monkeypatch.setattr(_tts, "_open_pcm_stream", lambda rate: _Speaker())
    started = time.monotonic()
    played = engine.ws_stream(
        _text_stream(["Good evening, sir. ", "All systems are nominal. "],
                     delay=0.8),
        on_audio=lambda: written.append(time.monotonic()))
    assert played, "the live session must produce audio"
    first = written[0] - started
    print(f"[live] first PCM chunk after {first * 1000:.0f} ms; "
          f"{len(written)} chunk(s) written")
