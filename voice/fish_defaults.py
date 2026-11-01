"""Fish Audio constants — pure data, no imports, no side effects.

Both ``voice/tts.py`` (the engine) and ``voice/providers_config.py`` (the
settings/migration layer) need to agree on the shipped model, voice, output
format, latency mode and the values Fish Audio actually accepts. Keeping them
here means the two can never drift, and this module stays importable in tests
without pulling in sounddevice/numpy.

Verified against the Fish Audio OpenAPI (``POST /v1/tts``):

* ``model`` is a request **header** whose enum is ``s1``, ``s2-pro``,
  ``s2.1-pro``, ``s2.1-pro-free``, ``drama-3``. An omitted or unrecognised
  value falls back to ``s2.1-pro`` server-side — which is why an unknown model
  must be rejected locally instead of being sent and silently replaced.
* ``format`` is ``wav`` | ``pcm`` | ``mp3`` | ``opus``.
* ``latency`` is ``low`` | ``normal`` | ``balanced``.
"""

from __future__ import annotations

FISH_DEFAULT_MODEL = "s2.1-pro-free"
FISH_DEFAULT_VOICE = "612b878b113047d9a770c069c8b4fdfe"
FISH_DEFAULT_FORMAT = "opus"
FISH_DEFAULT_LATENCY = "normal"
FISH_DEFAULT_ENDPOINT = "https://api.fish.audio/v1/tts"

FISH_MODELS = ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3")
FISH_FORMATS = ("pcm", "wav", "mp3", "opus")
FISH_LATENCIES = ("low", "normal", "balanced")

#: Human labels for the Settings format picker. The first word is what the
#: user reads; the parenthesised part states its real trade-off, verified
#: against the OpenAPI (Opus 48 kHz/mono, MP3 44.1 kHz, PCM/WAV 44.1 kHz 16-bit).
FISH_FORMAT_LABELS = (
    ("opus", "Opus (Fast)"),
    ("mp3", "MP3"),
    ("wav", "WAV"),
    ("pcm", "PCM (raw stream)"),
)

FISH_LATENCY_LABELS = (
    ("normal", "Normal (best quality)"),
    ("balanced", "Balanced (reduced latency)"),
    ("low", "Low (lowest latency)"),
)


def normalize_model(value: str) -> str:
    """Return a verified model id, or ``""`` when the value is not supported.

    An empty value means "use the shipped default". A supported value is
    returned unchanged. Anything else — including a voice ID typed into the
    model field — returns ``""`` so callers can raise instead of letting the
    server silently fall back to ``s2.1-pro``.
    """
    text = str(value or "").strip()
    return text if text in FISH_MODELS else ""


def looks_like_voice_id(value: str) -> bool:
    """A 24+ character lowercase hex string is a Fish voice/model UUID."""
    text = str(value or "").strip()
    return len(text) >= 20 and all(ch in "0123456789abcdefABCDEF" for ch in text)
