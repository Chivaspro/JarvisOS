"""Latency & behaviour fixes for the Gemini chat/voice pipelines.

Each test pins one of the fixes that made replies slow or context-blind:

* ``model_router.generate`` no longer sleeps AFTER the final failed attempt
  (up to 4 s of dead delay before the error reply).
* the plain HTTP providers now receive the recent conversation, bounded, so a
  follow-up is answered with context instead of from nothing.
* ``observe_utterance`` moves durable-fact promotion (the sentence-transformers
  cold load) off the conversation thread — and keeps an explicit synchronous
  form for callers that need the store settled.
* the wake config is cached on (mtime, size): the microphone callback used to
  re-read + re-parse config/api_keys.json on every ~64 ms block.
* native (gemini) voice mode resolves narration/notice lines to an OFFLINE
  engine instead of the EdgeTTS fall-through (a network round trip per line).
* the wiring in main.py: tool batches run concurrently, a failed tool never
  fires an EXTRA model turn, memory confirmations no longer force one either,
  queued typed messages are answered before the greeting, and a VAD downgrade
  is never silent.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


# ── model_router: no dead sleep after the final attempt ──────────────────────

def test_generate_retries_another_model_but_never_sleeps_after_the_last(
        monkeypatch):
    from app import model_router as mr

    sleeps: list[float] = []
    used: list[str] = []

    def fake_healthy(role, exclude=()):
        for model in ("m1", "m2", "m3"):
            if model not in exclude:
                return model
        return "m1"          # everything excluded — least-bad primary

    class _Models:
        @staticmethod
        def generate_content(**kwargs):
            used.append(kwargs["model"])
            raise RuntimeError("boom")

    monkeypatch.setattr(mr, "healthy_model_for", fake_healthy)
    monkeypatch.setattr(mr, "classify_error", lambda exc: "provider")
    monkeypatch.setattr(mr, "note_result", lambda *a, **k: None)
    monkeypatch.setattr(mr.time, "sleep", lambda s: sleeps.append(float(s)))

    with pytest.raises(RuntimeError):
        mr.generate(types.SimpleNamespace(models=_Models()),
                    contents="hi", max_attempts=3)

    assert used == ["m1", "m2", "m3"], "the retry ladder must still switch models"
    assert sleeps == [1.0, 2.0], (
        "a sleep is due only BETWEEN attempts — the old code slept up to 4 s "
        "AFTER the final failure, delaying the error reply for nothing")


# ── plain providers: bounded history ─────────────────────────────────────────

def test_bounded_history_merges_roles_and_keeps_the_newest():
    from app.ai_providers import _bounded_history

    out = _bounded_history([
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "system", "content": "never forwarded"},
        {"role": "assistant", "content": "c"},
        {"role": "user", "content": "d"},
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == "a\nb"       # consecutive same-role merged

    alternating = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(20)
    ]
    tail = _bounded_history(alternating, max_items=4)
    assert [m["content"] for m in tail] == ["m16", "m17", "m18", "m19"]


def test_provider_chat_forwards_the_history_to_the_http_layer(monkeypatch):
    from app import ai_providers as aip

    seen: dict = {}

    def fake_openai(endpoint, api_key, model, message, timeout=60, system=None,
                    temperature=0.7, history=None):
        seen["history"] = history
        seen["message"] = message
        return "ok"

    monkeypatch.setattr(aip, "_openai_chat", fake_openai)
    cfg = {"ai_provider": "openai", "ai_model": "gpt-x", "openai_api_key": "k"}
    history = [{"role": "user", "content": "first"},
               {"role": "assistant", "content": "one"}]

    assert aip.chat(cfg, "and the second one?", history=history) == "ok"
    assert seen["message"] == "and the second one?"
    assert seen["history"] == history


# ── brain_bridge: deferred promotion + cached wake config ────────────────────

def test_durable_promotion_runs_off_the_caller_thread(monkeypatch):
    import memory.policy as policy_mod
    from app import brain_bridge

    seen: dict = {}

    def fake_promote(text):
        seen["thread"] = threading.current_thread().name
        return {"stored": False}

    monkeypatch.setattr(policy_mod, "promote", fake_promote)
    monkeypatch.setitem(sys.modules, "memory.service", types.SimpleNamespace(
        get_memory_service=lambda: None,
        is_deferral=lambda text: False,
        detect_memory_command=lambda text: None,
    ))

    class _StubBrain:
        def observe(self, text): ...
        def note_turn(self, role, text): ...
        def route_skills(self, text, features=None, limit=4): return []

    monkeypatch.setattr(brain_bridge, "get_brain", lambda: _StubBrain())

    # Deferred (the app's default): the store write happens on a worker.
    brain_bridge.observe_utterance("I prefer tea in the morning.")
    deadline = time.time() + 2.0
    while "thread" not in seen and time.time() < deadline:
        time.sleep(0.02)
    assert seen.get("thread") == "memory-promote"

    # Explicit synchronous form: the caller's own thread does the write.
    brain_bridge.observe_utterance("I prefer tea in the morning.",
                                   defer_memory=False)
    assert seen["thread"] == threading.current_thread().name


def test_wake_config_is_cached_and_re_reads_only_after_a_change(
        tmp_path, monkeypatch):
    from app import brain_bridge

    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({"features": {"wake_word": True}}),
                   encoding="utf-8")
    monkeypatch.setattr(brain_bridge, "_wake_config_path", lambda: cfg)
    monkeypatch.setattr(brain_bridge, "_wake_cfg_cache", None)

    calls = {"n": 0}
    real_read = brain_bridge._read_json

    def counting_read(path):
        calls["n"] += 1
        return real_read(path)

    monkeypatch.setattr(brain_bridge, "_read_json", counting_read)

    assert brain_bridge.wake_enabled() is True
    for _ in range(10):
        brain_bridge.wake_enabled()
    assert calls["n"] == 1, (
        "the microphone callback must not re-read + re-parse the config "
        "on every audio block")

    # A Settings write changes the file: the very next check sees it.
    cfg.write_text(json.dumps({"features": {"wake_word": False}}),
                   encoding="utf-8")
    os.utime(cfg, (time.time() + 2, time.time() + 2))
    assert brain_bridge.wake_enabled() is False
    assert calls["n"] == 2


# ── TTS: side lines never fall into the network engine in gemini mode ────────

def test_gemini_side_lines_resolve_to_an_offline_engine(monkeypatch):
    tts = pytest.importorskip("voice.tts")

    built: dict = {}

    class _FakePlayer:
        def __init__(self, factory=None, name="", **kw):
            built["engine"] = factory() if factory else None
            built["name"] = name

    monkeypatch.setattr(tts, "TTSPlayer", _FakePlayer)
    monkeypatch.setattr(tts, "_TTS_CACHE", {})
    monkeypatch.setattr(tts, "load_voice_config", lambda: {})

    tts.create_tts_player({"voice_output_mode": "gemini"})

    engine = built["engine"]
    assert engine is not None and built["name"] == "gemini"
    assert not isinstance(engine, tts.EdgeTTSEngine), (
        "narration/notice lines in native voice mode must not do a network "
        "TTS round trip")


# ── main.py wiring (source-pinned) ───────────────────────────────────────────

def test_tool_calls_in_one_batch_run_concurrently():
    start = MAIN.index("if response.tool_call:")
    end = MAIN.index("await self.session.send_tool_response", start)
    assert "asyncio.gather" in MAIN[start:end], (
        "a tool batch must not wait for the slowest tool N times over")


def test_speak_error_never_fires_an_extra_model_turn():
    start = MAIN.index("def speak_error")
    end = MAIN.index("def _note_turn_latency", start)
    body = MAIN[start:end]
    assert "self.speak(" not in body, (
        "the failure already travels back in the FunctionResponse — a second "
        "turn doubled the wait after every failed tool")


def test_memory_confirmations_do_not_force_an_extra_turn():
    # The typed path executes the command itself and passes the outcome on,
    # so forget/remember are confirmed from the canned backend line.
    assert "require_reply=False," in MAIN
    assert "precomputed=_res if _res else None" in MAIN


def test_queued_messages_are_answered_before_the_greeting():
    anchor = MAIN.index('if getattr(self, "_pending_text_commands", None):')
    flush_at = MAIN.index("self._flush_pending_text_commands()", anchor)
    briefing_at = MAIN.index("tg.create_task(self._send_startup_briefing())",
                             anchor)
    assert flush_at < briefing_at, (
        "a message typed during connect must be answered instead of parked "
        "behind the greeting")


def test_a_vad_downgrade_is_never_silent():
    assert "Voice tuning rejected" in MAIN, (
        "dropping the low-latency VAD adds ~800 ms to every voice reply — "
        "that must be visible in the log (the Latency report parses it)")


def test_advisory_warmups_are_deferred_not_booted():
    assert "laya_decision.initialize(background=True)" not in MAIN, (
        "Laya's 78-164 s load must not run at boot any more")
    assert "_embeddings._load_encoder()" in MAIN, (
        "the embedding model is pre-warmed on the deferred worker")
