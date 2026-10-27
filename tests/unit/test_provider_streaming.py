"""Provider answers now stream: text and voice start before the reply is done.

Two layers are pinned here.

1. ``app.ai_providers.chat_stream`` - the SSE parser for the OpenAI-compatible
   and Anthropic shapes, driven by fake HTTP responses (no network): frames in
   order, keep-alives and malformed frames ignored, ``[DONE]``/``message_stop``
   ending the stream, cancellation, and the SAME configuration validation as
   the one-shot call.

2. ``main.py::_answer_via_provider`` - the turn wiring: deltas reach the screen
   and the speech sink in order, the turn is closed once and never logged
   twice, a stream that produced nothing falls back to the one-shot call, and
   with realtime disabled the synchronized whole-reply behaviour is kept.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── helpers ──────────────────────────────────────────────────────────────────

class FakeResponse:
    """Stands in for ``requests.Response`` with ``stream=True``."""

    def __init__(self, lines, status=200, raise_for_status: bool = False):
        self._lines = list(lines)
        self.status_code = status
        self._raise = raise_for_status

    def raise_for_status(self):
        if self._raise:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line if isinstance(line, str) else line.decode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse(*payloads):
    """SSE lines as a provider sends them (data: prefix, blank separators)."""
    out = []
    for payload in payloads:
        out.append(f"data: {payload}")
        out.append("")
    return out


def _openai_delta(text):
    return json.dumps({"choices": [{"delta": {"content": text}}]})


def _install_post(monkeypatch, response):
    import requests
    import app.ai_providers as aip

    seen: dict = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["stream"] = kwargs.get("stream")
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    return seen


OAI = {"ai_provider": "openai", "ai_model": "gpt-x", "openai_api_key": "k"}
ANT = {"ai_provider": "anthropic", "ai_model": "claude-x",
       "anthropic_api_key": "k"}


# ── the OpenAI-compatible stream ─────────────────────────────────────────────

def test_openai_stream_yields_deltas_in_order(monkeypatch):
    from app import ai_providers as aip

    response = FakeResponse(_sse(_openai_delta("Good "), _openai_delta("evening, "),
                                 _openai_delta("sir."), "[DONE]"))
    seen = _install_post(monkeypatch, response)

    chunks = list(aip.chat_stream(OAI, "hello"))

    assert chunks == ["Good ", "evening, ", "sir."]
    assert seen["stream"] is True, "the request must actually ask for a stream"
    assert seen["json"]["messages"][-1] == {"role": "user", "content": "hello"}
    assert seen["url"].endswith("/chat/completions")


def test_openai_stream_ignores_keepalives_and_malformed_frames(monkeypatch):
    from app import ai_providers as aip

    response = FakeResponse(["", ": keep-alive", "data: {not json",
                             "event: ping"] + _sse(_openai_delta("Still here.")) + ["data: [DONE]"])
    _install_post(monkeypatch, response)

    assert list(aip.chat_stream(OAI, "hello")) == ["Still here."]


def test_openai_stream_accepts_a_final_message_frame(monkeypatch):
    """Some gateways stream nothing and send the whole answer on the last
    frame — that answer must not be lost."""
    from app import ai_providers as aip

    final = json.dumps({"choices": [{"delta": {}, "message": {"content": "Complete."}}]})
    response = FakeResponse(_sse(final, "[DONE]"))
    _install_post(monkeypatch, response)

    assert list(aip.chat_stream(OAI, "hello")) == ["Complete."]


def test_openai_stream_without_a_done_marker_still_ends(monkeypatch):
    from app import ai_providers as aip

    response = FakeResponse(_sse(_openai_delta("Ends when the connection does.")))
    _install_post(monkeypatch, response)

    assert list(aip.chat_stream(OAI, "hello")) == ["Ends when the connection does."]


def test_stream_can_be_cancelled_between_frames(monkeypatch):
    import threading
    from app import ai_providers as aip

    cancel = threading.Event()
    response = FakeResponse(_sse(_openai_delta("one "), _openai_delta("two "),
                                 _openai_delta("three "), "[DONE]"))
    _install_post(monkeypatch, response)

    seen: list[str] = []
    for chunk in aip.chat_stream(OAI, "hello", cancel=cancel):
        seen.append(chunk)
        cancel.set()

    assert seen == ["one "], "an interrupted stream stops at the next frame"


def test_an_http_error_raises_before_any_text(monkeypatch):
    from app import ai_providers as aip

    _install_post(monkeypatch, FakeResponse([], status=429, raise_for_status=True))

    with pytest.raises(Exception):
        list(aip.chat_stream(OAI, "hello"))


# ── the Anthropic stream ─────────────────────────────────────────────────────

def test_anthropic_stream_yields_text_deltas(monkeypatch):
    from app import ai_providers as aip

    events = [
        {"type": "message_start"},
        {"type": "content_block_start"},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Sir, "}},
        {"type": "ping"},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "all is well."}},
        {"type": "content_block_stop"},
        {"type": "message_stop"},
    ]
    _install_post(monkeypatch, FakeResponse(_sse(*[json.dumps(e) for e in events])))

    assert list(aip.chat_stream(ANT, "status")) == ["Sir, ", "all is well."]


# ── one validation for both calls ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """A unit test must never reach a provider. Any real request fails loudly."""
    import requests

    def _forbid(*args, **kwargs):
        raise AssertionError("a unit test tried to make a real HTTP request")

    monkeypatch.setattr(requests, "post", _forbid)
    yield


@pytest.mark.parametrize("cfg", [
    {"ai_provider": "gemini", "ai_model": "gemini-3.8-live"},
    {"ai_provider": "openai", "ai_model": "gpt-x"},                     # no key
    # 'custom' has no default model list, so an empty model really is refused
    {"ai_provider": "custom", "ai_model": "", "custom_ai_api_key": "k",
     "custom_ai_base_url": "https://example.invalid/v1"},
    {"ai_provider": "custom", "ai_model": "x", "custom_ai_api_key": "k",
     "custom_ai_base_url": "not-a-url"},
])
def test_stream_and_oneshot_reject_the_same_configurations(cfg):
    from app import ai_providers as aip

    with pytest.raises(ValueError) as one_shot:
        aip.chat(cfg, "hello")
    with pytest.raises(ValueError) as streamed:
        list(aip.chat_stream(cfg, "hello"))

    assert str(one_shot.value) == str(streamed.value), (
        "a configuration must fail identically on both paths")


def test_chat_still_works_after_the_validation_refactor(monkeypatch):
    from app import ai_providers as aip

    seen: dict = {}

    def fake_openai(endpoint, api_key, model, message, timeout=60, system=None,
                    temperature=0.7, history=None):
        seen["message"] = message
        return "ok"

    monkeypatch.setattr(aip, "_openai_chat", fake_openai)
    assert aip.chat(OAI, "hello") == "ok"
    assert seen["message"] == "hello"


# ── the turn wiring in main.py ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def main():
    return pytest.importorskip("main")


class FakeSink:
    def __init__(self):
        self.fed: list[str] = []
        self.flushes = 0
        self.prepares = 0

    def feed(self, text):
        self.fed.append(str(text))
        return 1

    def flush(self):
        self.flushes += 1
        return 1

    def prepare(self, timeout=6.0):
        self.prepares += 1
        return True

    def interrupt(self, log=True):
        return 0

    def is_speaking(self):
        return False


class FakeUI:
    def __init__(self):
        self.streamed: list[str] = []
        self.finished = 0
        self.logged: list[str] = []

    def stream_assistant_text(self, chunk):
        self.streamed.append(str(chunk))

    def finish_assistant_stream(self):
        self.finished += 1

    def write_log(self, text):
        self.logged.append(str(text))


def _provider_host(main, sink=None, streamed_flags=True):
    host = main.JarvisLive.__new__(main.JarvisLive)
    host.ui = FakeUI()
    host._speech_stream = sink if sink is not None else FakeSink()
    host._stream_gate = ""
    host._stream_buffer = ""
    host._live_out_fed = ""
    host._startup_greeting_text = None
    host._session_log = []
    host._asst_name = "JARVIS"
    host._turn_task_id = None
    host._loop = None
    host.session = None
    host.voice_mode = lambda: "local"
    host._speech_sink = lambda: host._speech_stream
    host._reply_stream_enabled = lambda: bool(streamed_flags)
    host.set_speaking = lambda value: None
    spoken: list[str] = []
    host.speak_local = lambda text, **kw: spoken.append(str(text))
    host._spoken = spoken
    host._dashboard = None
    host._timeline = main._timeline
    return host, host._speech_stream


@pytest.fixture(autouse=True)
def _no_task_registry(monkeypatch):
    """The task registry writes to a real SQLite store; a unit test must not."""
    try:
        import main as main_mod
        monkeypatch.setattr(main_mod, "_task_registry", lambda: None, raising=False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _quiet_brain_bridge(monkeypatch):
    try:
        from core import brain_bridge
        monkeypatch.setattr(brain_bridge, "memory_system_prompt",
                            lambda *a, **k: "", raising=False)
        monkeypatch.setattr(brain_bridge, "record_reply",
                            lambda *a, **k: None, raising=False)
        monkeypatch.setattr(brain_bridge, "observe_utterance",
                            lambda *a, **k: None, raising=False)
    except Exception:
        pass
    yield


def test_provider_deltas_reach_the_screen_and_the_voice_in_order(main, monkeypatch):
    from app import ai_providers as aip
    host, sink = _provider_host(main)
    monkeypatch.setattr(aip, "chat_stream",
                        lambda cfg, text, **kw: iter(["Good ", "evening, ", "sir."]))

    host._answer_via_provider("hello", OAI)

    assert host.ui.streamed == ["Good ", "evening, ", "sir."]
    assert sink.fed == ["Good ", "evening, ", "sir."]
    assert host.ui.finished == 1 and sink.flushes == 1
    # A SYS progress line is expected; the REPLY must not be logged again.
    assert [line for line in host.ui.logged if line.upper().startswith("JARVIS:")] == [], (
        "a streamed reply must not be logged again through the whole-reply path")


def test_a_streamed_provider_turn_is_recorded_once(main, monkeypatch):
    from app import ai_providers as aip
    host, _ = _provider_host(main)
    monkeypatch.setattr(aip, "chat_stream",
                        lambda cfg, text, **kw: iter(["All systems nominal."]))

    host._answer_via_provider("status", OAI)

    assert host._session_log == ["assistant: All systems nominal."]
    assert host._spoken == [], "the voice is the sink's job on the streamed path"


def test_a_stream_that_produces_nothing_falls_back_to_the_one_shot_call(main,
                                                                       monkeypatch):
    from app import ai_providers as aip

    host, sink = _provider_host(main)
    asked: list[str] = []

    def empty_stream(cfg, text, **kw):
        raise RuntimeError("connection reset before any frame")
        yield  # pragma: no cover - makes this a generator

    def one_shot(cfg, text, **kw):
        asked.append(text)
        return "Answered the ordinary way."

    monkeypatch.setattr(aip, "chat_stream", empty_stream)
    monkeypatch.setattr(aip, "chat", one_shot)

    host._answer_via_provider("hello", OAI)

    assert asked == ["hello"], "the one-shot call must be the fallback"
    assert sink.fed == [], "nothing was streamed"
    assert any("Answered the ordinary way." in line for line in host.ui.logged)
    assert host._spoken == ["Answered the ordinary way."], (
        "the fallback path keeps the voice")


def test_a_mid_stream_failure_keeps_what_was_already_said(main, monkeypatch):
    from app import ai_providers as aip

    host, sink = _provider_host(main)
    asked: list[str] = []

    def broken_stream(cfg, text, **kw):
        yield "The reactor is "
        raise RuntimeError("socket closed")

    monkeypatch.setattr(aip, "chat_stream", broken_stream)
    monkeypatch.setattr(aip, "chat", lambda cfg, text, **kw: asked.append(text))

    host._answer_via_provider("report", OAI)

    assert "".join(host.ui.streamed) == "The reactor is ", (
        "what the user already read and heard is kept")
    assert asked == [], "the answer is never re-asked after it was partially spoken"
    assert any("partial" in line.lower() for line in host.ui.logged), (
        "the interruption is reported honestly")
    assert host._spoken == []


def test_realtime_off_publishes_the_complete_reply_with_its_audio(main,
                                                                  monkeypatch):
    """The synchronized fallback: no piecemeal text, one whole-reply call."""
    from app import ai_providers as aip

    host, sink = _provider_host(main, streamed_flags=False)
    monkeypatch.setattr(aip, "chat_stream",
                        lambda cfg, text, **kw: itertools_error())
    monkeypatch.setattr(aip, "chat", lambda cfg, text, **kw: "The complete answer.")

    host._answer_via_provider("hello", OAI)

    assert sink.fed == [] and host.ui.streamed == []
    assert any("The complete answer." in line for line in host.ui.logged)
    assert host._spoken == ["The complete answer."]


def itertools_error():        # pragma: no cover - must never run
    raise AssertionError("chat_stream must not be used when realtime is off")
