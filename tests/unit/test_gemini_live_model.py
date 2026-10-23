"""Gemini Live voice model — the setting must match the runtime (defect 5).

The voice session used to connect with whatever ``ai_model`` said, and
``Settings ▸ AI`` offered models the Live API refuses outright (verified
against the real API: "models/gemini-2.0-flash-001 … is not supported for
bidiGenerateContent"). Selecting one of those did not change the voice — it
killed it, with the failure surfacing as a reconnect loop.

2026-10-17: Google's deprecations page lists the previous default
(``models/gemini-2.5-flash-native-audio-preview-12-2025``) for shutdown on
2026-11-17, with ``gemini-3.8-live`` as the recommended replacement. These
tests pin the new default, the retired-id migration (an existing install must be
moved off a dead model with a REPORTED substitution), and the runtime wiring.

No network is involved: the model-name rules are what is under test, and the
live round-trip itself is a documented user-run smoke test (it needs a real
key, a microphone and a speaker).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app import ai_providers as aip  # noqa: E402

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
SCREEN = (ROOT / "actions" / "screen_processor.py").read_text(encoding="utf-8")
HYBRID = (ROOT / "voice" / "hybrid_voice.py").read_text(encoding="utf-8")
DIAGNOSTICS = (ROOT / "observability" / "diagnostics.py").read_text(encoding="utf-8")

CURRENT = "gemini-3.8-live"
RETIRED = "models/gemini-2.5-flash-native-audio-preview-12-2025"


# ── the default and the offered list ─────────────────────────────────────────

def test_the_current_supported_live_model_is_the_default():
    assert aip.GEMINI_DEFAULT_MODEL == CURRENT
    assert CURRENT in aip.GEMINI_LIVE_MODELS
    assert aip.is_live_model(CURRENT)


def test_the_retired_native_audio_preview_is_not_offered():
    # Shutdown 2026-11-17 — offering it in Settings would kill the voice.
    for model in aip.GEMINI_LIVE_MODELS:
        assert model not in aip.RETIRED_LIVE_MODELS
    assert RETIRED in aip.RETIRED_LIVE_MODELS


def test_every_offered_gemini_model_can_actually_serve_voice():
    for model in aip.models_for("gemini"):
        assert aip.is_live_model(model), f"{model} cannot drive the Live session"


# ── the resolution rule ──────────────────────────────────────────────────────

def test_a_stored_live_model_is_passed_through():
    model, note = aip.resolve_live_model({"ai_provider": "gemini",
                                          "ai_model": CURRENT})
    assert model == CURRENT and note == ""


def test_a_retired_stored_model_is_migrated_and_reported():
    model, note = aip.resolve_live_model({"ai_provider": "gemini",
                                          "ai_model": RETIRED})
    assert model == CURRENT
    assert note and "retired" in note.lower() and CURRENT in note


def test_a_retired_explicit_live_model_is_migrated_and_reported():
    model, note = aip.resolve_live_model({"live_model": RETIRED})
    assert model == CURRENT and note and "retired" in note.lower()


def test_a_non_live_choice_is_replaced_and_reported_not_swallowed():
    """§16: no silent fallback. The voice survives AND says what happened."""
    model, note = aip.resolve_live_model({"ai_provider": "gemini",
                                          "ai_model": "models/gemini-2.0-flash-001"})
    assert model == CURRENT
    assert note and "gemini-2.0-flash-001" in note and "not Live-capable" in note


def test_missing_model_config_still_lands_on_a_working_voice_model():
    model, note = aip.resolve_live_model({})
    assert model == CURRENT and note == ""
    # A non-Gemini text provider must not leave the voice without a model.
    model, note = aip.resolve_live_model({"ai_provider": "openai",
                                          "ai_model": "gpt-4o"})
    assert model == CURRENT and note == ""


def test_explicit_live_model_wins_and_bogus_explicit_names_are_reported():
    model, note = aip.resolve_live_model({
        "live_model": "gemini-3.8-live-extended-thinking",
        "ai_model": CURRENT})
    assert model == "gemini-3.8-live-extended-thinking" and note == ""
    model, note = aip.resolve_live_model({"live_model": "gemini-3.8-pro"})
    assert model == CURRENT and "not a real-time voice model" in note


# ── runtime wiring (pinned in the file that owns it) ─────────────────────────

def test_the_live_session_uses_the_resolved_model_and_reports_substitutions():
    assert "resolve_live_model(_acfg)" in MAIN
    start = MAIN.index("_live_model, _live_note = _aip.resolve_live_model")
    body = MAIN[start:start + 900]
    assert "if _live_note:" in body
    assert "write_log" in body, "a substituted voice model must be visible to the user"
    assert "client.aio.live.connect(model=_live_model" in MAIN


def test_no_live_session_pins_the_retired_model():
    assert "gemini-2.5-flash-native-audio-preview-12-2025" not in MAIN
    assert "gemini-2.5-flash-native-audio-preview-12-2025" not in SCREEN
    assert "gemini-2.5-flash-native-audio-preview-12-2025" not in HYBRID


def test_every_live_session_reads_the_one_central_default():
    # One source of truth: no module re-declares the model string.
    assert "from app.ai_providers import GEMINI_DEFAULT_MODEL as LIVE_MODEL" in MAIN
    assert "from app.ai_providers import GEMINI_DEFAULT_MODEL as _LIVE_MODEL" in SCREEN
    assert ("from app.ai_providers import GEMINI_DEFAULT_MODEL "
            "as _GEMINI_LIVE_MODEL") in HYBRID


def test_config_pins_blocking_tool_calling_for_the_stable_model():
    # Gemini 3.8 Live defaults function calling to NON_BLOCKING; this client
    # answers a tool call in the same turn, so the pin must be present — and it
    # must be droppable, or a server that rejects the field would fail every
    # reconnect forever.
    assert 'decl = {**decl, "behavior": "BLOCKING"}' in MAIN
    assert "self._pin_blocking_tools" in MAIN
    assert "_pin_blocking_tools = False" in MAIN


def test_config_no_longer_sends_the_removed_affective_dialog_field():
    # Gemini 3.8 Live removed enable_affective_dialog; sending it failed the
    # whole connect, so this client must not set it any more.
    assert 'cfg["enable_affective_dialog"]' not in MAIN


def test_diagnostics_reports_the_real_live_model():
    assert "live_voice_model=" in DIAGNOSTICS
    assert "resolve_live_model(cfg)" in DIAGNOSTICS


# ── the AI tab's TEST CONNECTION is a real probe ──────────────────────────────
# It used to return "Gemini voice model ready: <model>" from the key simply
# being non-empty, so a dead key was certified and the voice then failed. The
# one Gemini probe lives in services.hub (it knows the API's 10s deadline
# minimum); these tests pin the wiring to it.

_GM = "AQ." + "x" * 40


def test_gemini_test_connection_asks_the_hub_for_a_real_probe(monkeypatch):
    from services import hub
    calls: list = []

    def _fake_probe(service, config=None, timeout=8.0):
        calls.append((service, dict(config or {}), timeout))
        return {"state": "ready", "ok": True, "model": "gemini-flash-latest",
                "detail": "The Gemini key answered."}

    monkeypatch.setattr(hub, "probe", _fake_probe)
    reply = aip.test_connection({"ai_provider": "gemini", "gemini_api_key": _GM})
    assert calls and calls[0][0] == "gemini"
    assert calls[0][1]["gemini_api_key"] == _GM
    assert "gemini-flash-latest" in reply
    # The model that will actually drive the voice session is named.
    assert aip.GEMINI_DEFAULT_MODEL in reply


def test_a_failed_gemini_probe_raises_instead_of_being_certified(monkeypatch):
    from services import hub
    monkeypatch.setattr(hub, "probe", lambda *a, **k: {
        "state": "unavailable", "ok": False,
        "detail": "the key was rejected by Google"})
    with pytest.raises(RuntimeError) as exc:
        aip.test_connection({"ai_provider": "gemini", "gemini_api_key": _GM})
    assert "rejected" in str(exc.value)


def test_gemini_test_connection_still_refuses_without_a_key():
    with pytest.raises(ValueError):
        aip.test_connection({"ai_provider": "gemini", "gemini_api_key": ""})
