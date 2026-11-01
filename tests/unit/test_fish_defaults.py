"""Fish Audio: shipped defaults, no silent substitution, format plumbing.

The Fish OpenAPI declares ``model`` as a request **header** whose unknown
values fall back to ``s2.1-pro`` server-side. These tests pin the consequences:

* the shipped defaults are the verified ones (model / voice / Opus),
* an unsupported model, format or latency is refused locally instead of being
  silently rewritten,
* the model really travels in the header (never only in the body),
* the configured format is the one requested and decoded,
* a data reset seeds exactly those defaults so they survive a fresh start.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import voice.tts as tts  # noqa: E402
from voice.fish_defaults import (  # noqa: E402
    FISH_DEFAULT_FORMAT,
    FISH_DEFAULT_LATENCY,
    FISH_DEFAULT_MODEL,
    FISH_DEFAULT_VOICE,
    FISH_FORMATS,
    FISH_LATENCIES,
    FISH_MODELS,
)
from voice.providers_config import (  # noqa: E402
    migrate_to_providers,
    providers_block,
    save_provider,
    validate_provider,
)


# ── shipped defaults ─────────────────────────────────────────────────────────

def test_shipped_defaults_are_the_verified_values():
    assert FISH_DEFAULT_MODEL == "s2.1-pro-free"
    assert FISH_DEFAULT_VOICE == "612b878b113047d9a770c069c8b4fdfe"
    assert FISH_DEFAULT_FORMAT == "opus"
    assert FISH_DEFAULT_LATENCY == "normal"


def test_enums_match_the_openapi():
    assert FISH_MODELS == ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3")
    assert FISH_FORMATS == ("pcm", "wav", "mp3", "opus")
    assert FISH_LATENCIES == ("low", "normal", "balanced")


def test_engine_defaults_come_from_the_shared_constants():
    engine = tts.FishAudioTTSEngine(api_key="k")
    assert engine.model == FISH_DEFAULT_MODEL
    assert engine.reference_id == FISH_DEFAULT_VOICE
    assert engine.audio_format == FISH_DEFAULT_FORMAT
    assert engine.latency == FISH_DEFAULT_LATENCY


# ── no silent substitution ───────────────────────────────────────────────────

def test_unknown_model_is_refused_not_substituted():
    """The API would silently use s2.1-pro; the engine must refuse instead."""
    with pytest.raises(ValueError) as excinfo:
        tts.FishAudioTTSEngine(api_key="k", model_id="s9-hallucinated")
    assert "Unsupported Fish Audio model" in str(excinfo.value)


def test_voice_id_in_the_model_field_is_explained_not_rewritten():
    voice = "612b878b113047d9a770c069c8b4fdfe"
    with pytest.raises(ValueError) as excinfo:
        tts.FishAudioTTSEngine(api_key="k", model_id=voice,
                               voice_id=FISH_DEFAULT_VOICE)
    assert "looks like a voice ID" in str(excinfo.value)


def test_unknown_format_and_latency_are_refused():
    with pytest.raises(ValueError):
        tts.FishAudioTTSEngine(api_key="k", audio_format="flac")
    with pytest.raises(ValueError):
        tts.FishAudioTTSEngine(api_key="k", latency="turbo")


def test_empty_voice_is_an_error_not_a_default():
    with pytest.raises(ValueError) as excinfo:
        tts.FishAudioTTSEngine(api_key="k", voice_id="")
    assert "needs a voice" in str(excinfo.value)


def test_every_verified_value_is_accepted():
    for model in FISH_MODELS:
        assert tts.FishAudioTTSEngine(api_key="k", model_id=model).model == model
    for fmt in FISH_FORMATS:
        assert tts.FishAudioTTSEngine(api_key="k", audio_format=fmt).audio_format == fmt
    for latency in FISH_LATENCIES:
        assert tts.FishAudioTTSEngine(api_key="k", latency=latency).latency == latency


# ── the model travels in the header ─────────────────────────────────────────

class _FakeResponse:
    status_code = 200
    content = b"audio"
    text = ""

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size=4096):
        yield self.content


def test_http_request_puts_the_model_in_the_header(monkeypatch):
    seen = {}

    def _post(url, json=None, headers=None, timeout=None, stream=False):
        seen["url"] = url
        seen["json"] = dict(json or {})
        seen["headers"] = dict(headers or {})
        return _FakeResponse()

    import requests
    monkeypatch.setattr(requests, "post", _post)
    engine = tts.FishAudioTTSEngine(api_key="k", model_id="drama-3",
                                    audio_format="opus", latency="low")
    assert engine._convert_http("hello") == b"audio"
    assert seen["headers"]["model"] == "drama-3"
    # The body must not duplicate the model: it belongs to the header only.
    assert "model" not in seen["json"]
    assert seen["json"]["format"] == "opus"
    assert seen["json"]["latency"] == "low"
    assert seen["json"]["reference_id"] == FISH_DEFAULT_VOICE


# ── the configured format is honoured ───────────────────────────────────────

def _frames(engine, monkeypatch, payload=b"RIFF....", pcm=(b"\x00\x01" * 64, 24000)):
    seen = {}

    class _Stream(_FakeResponse):
        content = payload

        def iter_content(self, chunk_size=4096):
            yield payload

    def _post(url, json=None, headers=None, timeout=None, stream=False):
        seen["json"] = dict(json or {})
        seen["stream"] = stream
        return _Stream()

    import requests
    monkeypatch.setattr(requests, "post", _post)
    if pcm is not None:
        monkeypatch.setattr(tts, "_decode_audio_to_pcm", lambda data: pcm)
    return list(engine._frame_iterator("hello")), seen


def test_pcm_format_streams_raw_without_a_decoder(monkeypatch):
    engine = tts.FishAudioTTSEngine(api_key="k", audio_format="pcm")
    frames, seen = _frames(engine, monkeypatch, payload=b"\x01\x02" * 10, pcm=None)
    assert seen["json"]["format"] == "pcm"
    assert frames and all(rate == 44100 for _data, rate in frames)
    assert b"".join(data for data, _ in frames) == b"\x01\x02" * 10


def test_opus_format_is_decoded_and_chunked(monkeypatch):
    engine = tts.FishAudioTTSEngine(api_key="k", audio_format="opus")
    pcm = (b"\x00" * 32768, 48000)
    frames, seen = _frames(engine, monkeypatch, payload=b"OggS-opus", pcm=pcm)
    assert seen["json"]["format"] == "opus"
    assert all(rate == 48000 for _data, rate in frames)
    assert b"".join(data for data, _ in frames) == pcm[0]


def test_opus_is_not_rewritten_to_mp3(monkeypatch):
    engine = tts.FishAudioTTSEngine(api_key="k", audio_format="opus")
    _chunks, seen = _frames(engine, monkeypatch, pcm=(b"\x00" * 8, 48000))
    assert seen["json"]["format"] == "opus"


# ── providers / settings / reset ────────────────────────────────────────────

def test_provider_defaults_are_the_shipped_fish_values():
    fish = providers_block({})["providers"]["fishAudio"]
    assert fish["model"] == FISH_DEFAULT_MODEL
    assert fish["voice"] == FISH_DEFAULT_VOICE
    assert fish["format"] == FISH_DEFAULT_FORMAT
    assert fish["latency"] == FISH_DEFAULT_LATENCY


def test_low_latency_is_no_longer_rewritten_to_balanced():
    cfg = {"fish_audio_latency": "low"}
    assert providers_block(cfg)["providers"]["fishAudio"]["latency"] == "low"
    migrated, _ = migrate_to_providers(cfg)
    assert migrated["voice"]["providers"]["fishAudio"]["latency"] == "low"


def test_migration_round_trip_keeps_the_new_defaults():
    migrated, changed = migrate_to_providers({})
    assert changed
    fish = migrated["voice"]["providers"]["fishAudio"]
    assert fish["model"] == FISH_DEFAULT_MODEL
    assert fish["voice"] == FISH_DEFAULT_VOICE
    assert fish["format"] == FISH_DEFAULT_FORMAT
    # A save of one field must not disturb the rest.
    saved = save_provider(migrated, "fishAudio", {"apiKey": "k"})
    again = providers_block(saved)["providers"]["fishAudio"]
    assert again["apiKey"] == "k"
    assert again["model"] == FISH_DEFAULT_MODEL
    assert again["format"] == FISH_DEFAULT_FORMAT


def test_validate_accepts_the_shipped_defaults():
    values = {
        "apiKey": "k",
        "endpoint": "https://api.fish.audio/v1/tts",
        "model": FISH_DEFAULT_MODEL,
        "voice": FISH_DEFAULT_VOICE,
        "format": FISH_DEFAULT_FORMAT,
        "latency": FISH_DEFAULT_LATENCY,
    }
    assert validate_provider("fishAudio", values) is None


def test_validate_refuses_values_the_api_would_silently_change():
    base = {"apiKey": "k", "voice": FISH_DEFAULT_VOICE}
    assert "Unknown Fish Audio model" in validate_provider(
        "fishAudio", {**base, "model": "gpt-5"})
    assert "Unknown Fish Audio format" in validate_provider(
        "fishAudio", {**base, "format": "flac"})
    assert "Unknown Fish Audio latency" in validate_provider(
        "fishAudio", {**base, "latency": "turbo"})


def test_data_reset_seeds_the_shipped_fish_defaults():
    import setup_jarvis

    seeded = setup_jarvis._fresh_config()
    assert seeded["fish_audio_model_id"] == FISH_DEFAULT_MODEL
    assert seeded["fish_audio_voice_id"] == FISH_DEFAULT_VOICE
    assert seeded["fish_audio_format"] == FISH_DEFAULT_FORMAT
    assert seeded["fish_audio_latency"] == FISH_DEFAULT_LATENCY
    fish = seeded["voice"]["providers"]["fishAudio"]
    assert fish["model"] == FISH_DEFAULT_MODEL
    assert fish["voice"] == FISH_DEFAULT_VOICE
    assert fish["format"] == FISH_DEFAULT_FORMAT
    # The seeded config is what the provider layer reports, unchanged.
    assert providers_block(seeded)["providers"]["fishAudio"] == fish


# ── decoding (real bytes, no network) ───────────────────────────────────────

def test_decode_reads_a_real_wav_payload():
    sf = pytest.importorskip("soundfile")
    import numpy as np

    rate = 24000
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(rate // 10) / rate))
    buf = io.BytesIO()
    sf.write(buf, tone.astype("float32"), rate, format="WAV", subtype="PCM_16")
    pcm, out_rate = tts._decode_audio_to_pcm(buf.getvalue())
    assert out_rate == rate
    assert len(pcm) % 2 == 0
    assert len(pcm) // 2 == len(tone)


def test_decode_opus_payload_when_the_codec_is_available():
    sf = pytest.importorskip("soundfile")
    import numpy as np

    rate = 48000
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(rate // 10) / rate))
    buf = io.BytesIO()
    try:
        sf.write(buf, tone.astype("float32"), rate, format="OGG", subtype="OPUS")
    except Exception as exc:  # libsndfile without Opus support
        pytest.skip(f"no Opus encoder: {exc}")
    pcm, out_rate = tts._decode_audio_to_pcm(buf.getvalue())
    assert out_rate == rate
    assert len(pcm) > 0


def test_decode_reports_a_useful_failure_for_garbage():
    with pytest.raises(RuntimeError) as excinfo:
        tts._decode_audio_to_pcm(b"not audio at all")
    assert "could not decode" in str(excinfo.value)
