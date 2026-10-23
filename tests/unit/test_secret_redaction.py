"""Secret redaction must cover every provider key — including future ones.

The exact-name set is the primary list; the suffix rule is the safety net that
makes a NEW integration (openai_api_key, deepseek_api_key, …) safe the day it is
added rather than the day it leaks. Over-redaction is pinned too: a token COUNT
must survive, so the rule is suffix-based, not substring-based.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import SECRET_FIELD_NAMES, redact_secrets  # noqa: E402


#: Provider/service credential names the app can store today.
KNOWN_PROVIDER_KEYS = [
    "gemini_api_key", "openai_api_key", "anthropic_api_key", "openrouter_api_key",
    "deepseek_api_key", "groq_api_key", "elevenlabs_api_key", "fish_audio_api_key",
    "mem0_api_key", "qdrant_api_key", "composio_api_key", "weather_api_key",
    "radar_api_key", "custom_ai_api_key", "spotify_client_secret",
    "spotify_access_token", "instagram_password", "smart_home_token",
    "connect_pairing_secret",
]


@pytest.mark.parametrize("key", KNOWN_PROVIDER_KEYS)
def test_every_known_provider_key_is_redacted(key):
    assert redact_secrets({key: "real-value-123"})[key] == "[REDACTED]"


@pytest.mark.parametrize("key", [
    "openai_api_key", "deepseek_api_key", "some_future_api_key",
    "x_access_token", "vendor_client_secret", "service_password",
    "widget_secret", "widget_token",
])
def test_a_future_integration_key_is_covered_by_the_suffix_rule(key):
    assert redact_secrets({key: "v"})[key] == "[REDACTED]"


def test_nested_blocks_are_redacted_recursively():
    payload = {"features": {"chat": True},
               "providers": {"openai": {"openai_api_key": "k", "model": "gpt"}}}
    out = redact_secrets(payload)
    assert out["providers"]["openai"]["openai_api_key"] == "[REDACTED]"
    assert out["providers"]["openai"]["model"] == "gpt"
    assert out["features"]["chat"] is True


def test_a_string_under_a_secret_hint_is_redacted():
    assert redact_secrets("sk-real", "anthropic_api_key") == "[REDACTED]"


def test_non_secret_keys_are_not_over_redacted():
    payload = {"max_tokens": 4096, "token_count": 12, "model": "gemini-3.8-live",
               "temperature": 0.7, "session_seconds": 30}
    assert redact_secrets(payload) == payload


def test_case_and_whitespace_are_handled():
    assert redact_secrets({"GEMINI_API_KEY": "x"})["GEMINI_API_KEY"] == "[REDACTED]"
    assert redact_secrets({"  api_key ": "x"})["  api_key "] == "[REDACTED]"


def test_the_explicit_set_is_lowercase_and_normalised():
    assert SECRET_FIELD_NAMES
    for name in SECRET_FIELD_NAMES:
        assert name == name.lower().strip(), name
