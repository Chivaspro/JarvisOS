"""Streamed cloud TTS: Fish Audio and ElevenLabs must start speaking sooner.

The slow behaviour being fixed was structural: the whole reply was downloaded
before a single sample was heard. These tests pin the two properties that make
the difference — the reply is split at sentence boundaries, and the NEXT
sentence's request is opened while the current sentence is still playing — plus
the safety net: any streaming failure falls back to the previous whole-reply
path instead of going silent, and an interruption can now cut a cloud voice
short (it used to be ignored entirely).

No real network, no real speaker: synthesis and playback are both faked.
"""
from __future__ import annotations

import inspect
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice import tts  # noqa: E402


# ── sentence splitting ─────────────────────────────────────────────────────

def test_splits_a_multi_sentence_reply():
    assert tts.split_sentences("Hello sir. How are you? Fine!") == [
        "Hello sir.", "How are you?", "Fine!"]


def test_blank_text_yields_nothing():
    assert tts.split_sentences("") == []
    assert tts.split_sentences("   \n ") == []


def test_a_run_on_sentence_is_broken_so_playback_can_start():
    long = ("This is a deliberately long clause, and it keeps going, and going, "
            "and going, with no full stop anywhere in sight at all")
    parts = tts.split_sentences(long, max_chars=60)
    assert len(parts) > 1
    assert all(len(p) <= 60 for p in parts)
    assert " ".join(parts).split() == long.split()   # nothing lost, nothing added


# ── pipelining ─────────────────────────────────────────────────────────────

def test_next_sentence_is_synthesised_while_the_current_one_plays(monkeypatch):
    """The pipeline property: network latency hides behind playback."""
    events: list = []
    playing = threading.Event()

    def fake_play(frames, cancel=None, on_audio=None, preopen_rate=None):
        gen = iter(frames)
        first = next(gen)
        events.append(("play", first[0].decode()))
        playing.set()                     # sentence 1 is now audible
        for chunk, _rate in gen:
            events.append(("play", chunk.decode()))
        return True

    def make_frames(sentence):
        if sentence == "Second one.":
            # Must already be playing sentence 1. With no prefetch (synthesise
            # then play, strictly in order) this would block until timeout.
            assert playing.wait(3.0), "no prefetch: sentence 2 started too late"
        events.append(("synth", sentence))
        yield (sentence.encode(), 24000)

    monkeypatch.setattr(tts, "_play_pcm_frames", fake_play)
    tts._play_sentence_stream("First one. Second one.", make_frames)
    assert events == [
        ("synth", "First one."), ("play", "First one."),
        ("synth", "Second one."), ("play", "Second one."),
    ]


def test_each_sentence_is_requested_separately(monkeypatch):
    asked: list = []

    def fake_play(frames, cancel=None, on_audio=None, preopen_rate=None):
        list(frames)
        return True

    monkeypatch.setattr(tts, "_play_pcm_frames", fake_play)

    def make_frames(sentence):
        asked.append(sentence)
        yield (b"\x00\x00", 24000)

    tts._play_sentence_stream("One. Two. Three.", make_frames)
    assert asked == ["One.", "Two.", "Three."]


def test_cancel_stops_a_streamed_reply(monkeypatch):
    """An interruption must cut a cloud voice short — it used to be ignored.

    The stop arrives WHILE sentence 1 is playing (the realistic case): sentence
    1 must be heard, and the already-prefetched sentence 2 must be dropped
    rather than spoken after the user asked for silence.
    """
    cancel = threading.Event()
    played: list = []

    def fake_play(frames, cancel=None, on_audio=None, preopen_rate=None):
        for chunk, _rate in frames:
            if cancel is not None and cancel.is_set():
                break
            played.append(chunk.decode())
            if chunk == b"One.":
                cancel.set()      # the user pressed stop, mid-sentence
        return True

    monkeypatch.setattr(tts, "_play_pcm_frames", fake_play)

    def make_frames(sentence):
        yield (sentence.encode(), 24000)

    tts._play_sentence_stream("One. Two. Three.", make_frames, cancel=cancel)
    assert played == ["One."]


def test_on_audio_fires_when_audio_really_starts(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tts, "_play_pcm_frames",
                        lambda frames, cancel=None, on_audio=None,
                        preopen_rate=None: (
                            [next(iter(frames))],
                            on_audio() if on_audio else None,
                            True)[-1])

    tts._play_sentence_stream("One.", lambda s: iter([(b"\x00\x00", 24000)]),
                              on_audio=lambda: calls.append(1))
    assert calls == [1]


def test_stream_error_with_no_audio_is_raised_for_the_fallback(monkeypatch):
    monkeypatch.setattr(tts, "_play_pcm_frames",
                        lambda frames, cancel=None, on_audio=None,
                        preopen_rate=None: (list(frames) and True))

    def make_frames(sentence):
        raise RuntimeError("HTTP 500")
        yield None      # pragma: no cover - makes this a generator

    with pytest.raises(RuntimeError):
        tts._play_sentence_stream("One.", make_frames)


def test_partial_audio_then_error_does_not_raise(monkeypatch):
    """Half a reply already reached the speaker: report, do not restart it."""
    def fake_play(frames, cancel=None, on_audio=None, preopen_rate=None):
        list(frames)
        return True

    monkeypatch.setattr(tts, "_play_pcm_frames", fake_play)

    def make_frames(sentence):
        if sentence == "Two.":
            raise RuntimeError("connection reset")
        yield (sentence.encode(), 24000)

    assert tts._play_sentence_stream("One. Two.", make_frames) is True


# ── WAV streaming (Fish Audio) ─────────────────────────────────────────────

def _wav_header(rate: int = 44100, extra: bytes = b"") -> bytes:
    return (b"RIFF" + b"\x00" * 4 + b"WAVE"
            + extra
            + b"fmt " + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
            + rate.to_bytes(4, "little")
            + (rate * 2).to_bytes(4, "little")
            + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
            + b"data" + b"\x00" * 4)


def test_wav_header_gives_the_real_sample_rate():
    rate, pcm = tts._strip_wav_header(_wav_header(44100) + b"PCMDATA", None)
    assert rate == 44100
    assert pcm == b"PCMDATA"


def test_wav_header_survives_extra_chunks_before_data():
    rate, pcm = tts._strip_wav_header(
        _wav_header(22050, extra=b"LIST" + (4).to_bytes(4, "little") + b"junk")
        + b"REAL", None)
    assert rate == 22050
    assert pcm == b"REAL"


def test_unreadable_header_degrades_to_the_default_rate():
    """No data chunk found: fall back to the canonical 44-byte header."""
    rate, pcm = tts._strip_wav_header(b"RIFF" + b"\x00" * 40 + b"AUDIO", None)
    assert rate == 24000
    assert pcm == b"AUDIO"


# ── provider wiring ────────────────────────────────────────────────────────

def test_cloud_engines_accept_cancel_and_on_audio():
    """TTSPlayer can now cut them short and time their first audio."""
    import inspect
    for engine_cls in (tts.ElevenLabsTTSEngine, tts.FishAudioTTSEngine):
        params = inspect.signature(engine_cls.speak).parameters
        assert "cancel" in params and "on_audio" in params


def test_elevenlabs_uses_the_streaming_endpoint_with_raw_pcm():
    """Raw PCM in, no container: audio can reach the speaker as it arrives."""
    eng = tts.ElevenLabsTTSEngine(api_key="k", voice_id="v" * 20)
    src = inspect.getsource(eng._frame_iterator)
    assert "/stream" in src
    assert "ELEVENLABS_PCM_FORMAT" in src
    assert "optimize_streaming_latency" in src
    assert tts.ELEVENLABS_PCM_FORMAT == "pcm_24000"         # 16-bit, 24 kHz, raw
    assert tts.ELEVENLABS_DEFAULT_MODEL == "eleven_flash_v2_5"


def test_elevenlabs_falls_back_to_the_whole_reply(monkeypatch):
    """A rejected streaming request must not mean a silent J.A.R.V.I.S."""
    eng = tts.ElevenLabsTTSEngine(api_key="k", voice_id="v" * 20)
    monkeypatch.setattr(tts, "_streaming_enabled", lambda: True)
    monkeypatch.setattr(eng, "_frame_iterator",
                        lambda s: (_ for _ in ()).throw(RuntimeError("HTTP 400")))
    monkeypatch.setattr(eng, "_fetch_whole", lambda text: b"WHOLE")
    played: list = []
    monkeypatch.setattr(tts, "_play_audio_bytes", lambda b: played.append(b))
    eng.speak("Hello sir.")
    assert played == [b"WHOLE"]


def test_fish_falls_back_to_the_whole_reply(monkeypatch):
    eng = tts.FishAudioTTSEngine(api_key="k", voice_id="v")
    monkeypatch.setattr(tts, "_streaming_enabled", lambda: True)
    monkeypatch.setattr(eng, "_frame_iterator",
                        lambda s: (_ for _ in ()).throw(RuntimeError("HTTP 404")))
    monkeypatch.setattr(eng, "_convert_sdk", lambda text: b"WHOLE")
    played: list = []
    monkeypatch.setattr(tts, "_play_audio_bytes", lambda b: played.append(b))
    eng.speak("Hello sir.")
    assert played == [b"WHOLE"]


def test_streaming_can_be_switched_off(monkeypatch):
    eng = tts.ElevenLabsTTSEngine(api_key="k", voice_id="v" * 20)
    monkeypatch.setattr(tts, "_streaming_enabled", lambda: False)
    monkeypatch.setattr(eng, "_frame_iterator",
                        lambda s: pytest.fail("streaming path used while OFF"))
    monkeypatch.setattr(eng, "_fetch_whole", lambda text: b"WHOLE")
    played: list = []
    monkeypatch.setattr(tts, "_play_audio_bytes", lambda b: played.append(b))
    eng.speak("Hello sir.")
    assert played == [b"WHOLE"]


def test_streaming_flag_reads_the_settings_layer(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "get_feature", lambda name, default=True: False)
    assert tts._streaming_enabled() is False
    monkeypatch.setattr(settings, "get_feature", lambda name, default=True: True)
    assert tts._streaming_enabled() is True


def test_empty_text_is_a_no_op_for_both_cloud_engines(monkeypatch):
    monkeypatch.setattr(tts, "_play_audio_bytes",
                        lambda b: pytest.fail("nothing to speak"))
    tts.ElevenLabsTTSEngine(api_key="k", voice_id="v" * 20).speak("   ")
    tts.FishAudioTTSEngine(api_key="k", voice_id="v").speak("")


# -- continuous local playback (Piper / Kokoro) -----------------------------

class _FakeOutputStream:
    """Stands in for sounddevice.RawOutputStream (no real device)."""

    def __init__(self, rate: int):
        self.rate = rate
        self.latency = 0.05
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))

    def stop(self):
        pass

    def close(self):
        self.closed = True


def test_local_chunks_share_one_output_stream(monkeypatch):
    """Per-chunk sd.play()/wait() seams are gone: ONE stream, back-to-back."""
    import numpy as np

    opened: list[_FakeOutputStream] = []

    def _open(rate):
        stream = _FakeOutputStream(int(rate))
        opened.append(stream)
        return stream

    monkeypatch.setattr(tts, "_open_pcm_stream", _open)
    frames = [(np.zeros(240, dtype=np.float32), 24000) for _ in range(3)]
    assert tts._play_np_frames(frames, preopen_rate=24000) is True
    assert len(opened) == 1, "the device must not be reopened between chunks"
    assert len(opened[0].writes) == 3
    assert all(len(w) == 480 for w in opened[0].writes)   # 240 samples x 2 bytes
    assert opened[0].closed


def test_local_interrupt_fades_out_instead_of_clicking(monkeypatch):
    import numpy as np

    stream = _FakeOutputStream(24000)
    monkeypatch.setattr(tts, "_open_pcm_stream", lambda rate: stream)
    cancel = threading.Event()

    def _frames():
        yield np.ones(240, dtype=np.float32), 24000
        cancel.set()
        yield np.ones(240, dtype=np.float32), 24000

    assert tts._play_np_frames(_frames(), cancel=cancel,
                               preopen_rate=24000) is True
    # one real chunk, then the 20 ms ramp to silence (480 samples = 960 bytes)
    assert len(stream.writes) == 2
    assert len(stream.writes[1]) == int(0.02 * 24000) * 2
    assert len(stream.writes[0]) == 480


def test_local_frames_survive_a_speaker_error(monkeypatch):
    import numpy as np

    class _Broken(_FakeOutputStream):
        def write(self, data):
            raise RuntimeError("device lost")

    monkeypatch.setattr(tts, "_open_pcm_stream", lambda rate: _Broken(rate))
    assert tts._play_np_frames(
        [(np.zeros(240, dtype=np.float32), 24000)]) is False
