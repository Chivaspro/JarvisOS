"""The speech firewall: display content can never reach TTS.

Previews, execution reports, code, operation cards, web results and tool results
are DISPLAY content. They are often full sentences, so nothing about their shape
can tell the voice system to stay silent — the *kind* must. These tests pin the
one explicit gate in ``main.py``:

* ``_speech_allowed(kind)`` is an explicit denylist (``_PREVIEW_KINDS``): only
  the preview kinds are blocked, every other kind keeps today's behaviour;
* ``_speak_stream`` (live sink) and ``_speak_line`` (whole line) both pass
  through it, while the same text with ``kind="reply"``/``"system"`` speaks;
* the real ``speak_local`` refuses a ``kind="preview"`` line before it touches
  the engine — all five existing positional callers stay valid;
* the agent execution report sink is display-only (coding card + activity log)
  and never opens a model turn through ``self.speak``;
* the system prompt tells the model to acknowledge a preview, not read it.

The display path is deliberately NOT involved here: gating a preview must never
remove it from the Chat Log (see tests/ui/test_preview_speech_isolation.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")

# A realistic report: complete sentences, exactly the shape that used to be
# spoken because nothing distinguished it from a conversational reply.
REPORT = (
    "PREVIEW — Fusion energy briefing\n"
    "The reactor design uses superconducting magnets. "
    "The team expects a net-energy demonstration in 2032. "
    "Full details are in the attached summary."
)
REPLY = "I've opened the website for previewing, sir."

PREVIEW_KINDS = ("preview", "report", "code", "operation", "web", "tool_result")


class FakeSink:
    """The live voice sink: records every fragment it is asked to speak."""

    def __init__(self):
        self.fed: list[str] = []

    def feed(self, text):
        self.fed.append(str(text))
        return 1

    def interrupt(self, log=True):
        return 0

    def is_speaking(self):
        return False


class FakeUI:
    """Only the non-spoken surfaces the agent report sink may use."""

    def __init__(self):
        self.coding: list[tuple] = []
        self.logs: list[str] = []

    def coding_activity(self, kind, payload=None):
        self.coding.append((str(kind), dict(payload or {})))

    def write_log(self, text):
        self.logs.append(str(text))


@pytest.fixture(scope="module")
def main():
    return pytest.importorskip("main")


def _host(main, sink=None):
    """A real JarvisLive with only the state the firewall touches."""
    host = main.JarvisLive.__new__(main.JarvisLive)
    sink = sink if sink is not None else FakeSink()
    spoken: list[str] = []
    host.ui = FakeUI()
    host._speech_stream = sink
    host._speech_sink = lambda: sink
    host._spoken = spoken
    # The real speak_local needs a live engine/lock; record the call instead so
    # the GATE is what each assertion measures.
    host.speak_local = (
        lambda text, force=False, mark_timeline=True, **kw: spoken.append(str(text)))
    return host, sink, spoken


def _bare_host(main, sink):
    """A host WITHOUT speak_local patched: the real method runs (and must gate)."""
    host = main.JarvisLive.__new__(main.JarvisLive)
    host.ui = FakeUI()
    host._speech_stream = sink
    host._speech_sink = lambda: sink
    return host


# ── the denylist ─────────────────────────────────────────────────────────────

def test_preview_kinds_are_the_documented_explicit_set():
    from main import _PREVIEW_KINDS

    assert isinstance(_PREVIEW_KINDS, frozenset)
    assert set(_PREVIEW_KINDS) == set(PREVIEW_KINDS), (
        "the denylist is explicit: adding/removing a kind changes what is voiced")


@pytest.mark.parametrize("kind", PREVIEW_KINDS)
def test_every_preview_kind_is_blocked(main, kind):
    host, _sink, _spoken = _host(main)
    assert host._speech_allowed(kind) is False


@pytest.mark.parametrize("kind", ["reply", "system", "narration", "greeting",
                                  "status", "", None, "unknown-kind"])
def test_every_other_kind_keeps_talking(main, kind):
    host, _sink, _spoken = _host(main)
    assert host._speech_allowed(kind) is True, (
        "an explicit denylist must never silence a speaker it does not know")


def test_the_kind_match_ignores_case_and_whitespace(main):
    host, _sink, _spoken = _host(main)
    assert host._speech_allowed("  PREVIEW  ") is False
    assert host._speech_allowed("Report\n") is False


# ── the live sink (streamed reply path) ──────────────────────────────────────

def test_a_preview_never_reaches_the_live_sink(main):
    host, sink, spoken = _host(main)

    host._speak_stream(REPORT, kind="preview")

    assert sink.fed == [], "a preview must never be fed to the voice sink"
    assert spoken == [], "and it must never start a local utterance either"


@pytest.mark.parametrize("kind", PREVIEW_KINDS)
def test_no_preview_kind_reaches_the_live_sink(main, kind):
    host, sink, _spoken = _host(main)
    host._speak_stream(REPORT, kind=kind)
    assert sink.fed == []


def test_a_reply_still_reaches_the_live_sink(main):
    host, sink, _spoken = _host(main)

    host._speak_stream(REPLY, kind="reply")
    host._speak_stream("And a second fragment.", kind="reply")

    assert sink.fed == [REPLY, "And a second fragment."]


def test_an_unknown_kind_still_reaches_the_live_sink(main):
    host, sink, _spoken = _host(main)

    host._speak_stream(REPLY, kind="some-future-kind")

    assert sink.fed == [REPLY], "back-compat: only the denylist is silent"


def test_the_default_stream_kind_is_the_reply(main):
    host, sink, _spoken = _host(main)

    host._speak_stream(REPLY)

    assert sink.fed == [REPLY]


def test_no_sink_is_not_an_error(main):
    host = main.JarvisLive.__new__(main.JarvisLive)
    host._speech_sink = lambda: None

    host._speak_stream(REPLY, kind="reply")     # must not raise


# ── whole lines (speak_local / _speak_line) ──────────────────────────────────

def test_a_report_line_is_never_spoken(main):
    host, _sink, spoken = _host(main)

    host._speak_line(REPORT, kind="report")

    assert spoken == []


@pytest.mark.parametrize("kind", PREVIEW_KINDS)
def test_no_preview_kind_is_ever_spoken_as_a_line(main, kind):
    host, _sink, spoken = _host(main)
    host._speak_line(REPORT, kind=kind)
    assert spoken == []


@pytest.mark.parametrize("kind", ["reply", "system", "narration"])
def test_a_conversational_line_still_speaks(main, kind):
    host, _sink, spoken = _host(main)

    host._speak_line(REPLY, kind=kind)

    assert spoken == [REPLY]


def test_an_unknown_line_kind_still_speaks(main):
    host, _sink, spoken = _host(main)

    host._speak_line(REPLY, kind="some-future-kind")

    assert spoken == [REPLY], "back-compat: only the denylist is silent"


def test_a_blank_line_is_not_spoken(main):
    host, _sink, spoken = _host(main)

    host._speak_line("   \n\t ", kind="system")

    assert spoken == []


def test_the_real_speak_local_gates_a_preview_before_the_engine(main, monkeypatch):
    """The REAL method: kind="preview" returns before any TTS work happens."""
    import core.tts as core_tts

    created: list = []
    monkeypatch.setattr(core_tts, "create_tts_player",
                        lambda cfg: created.append(cfg), raising=False)

    host = _bare_host(main, FakeSink())
    # A bare host has no _local_tts_lock/_local_tts_stop: reaching them would
    # raise, so the silence below proves the gate is the FIRST executable step.
    host.speak_local(REPORT, kind="preview")

    assert created == [], "the preview never even asked the engine to start"


def test_the_real_speak_local_still_accepts_positional_callers(main):
    """Five existing callers pass (text[, force[, mark_timeline]]) positionally."""
    import inspect

    params = list(inspect.signature(main.JarvisLive.speak_local).parameters.values())
    names = [p.name for p in params]
    assert names[:4] == ["self", "text", "force", "mark_timeline"]
    kind_param = params[4]
    assert kind_param.name == "kind"
    assert kind_param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "kind must be keyword-only so positional callers are untouched")
    assert kind_param.default == "system", (
        "the default is an allowed kind: no existing speaker is silenced")


# ── the agent execution-report sink ─────────────────────────────────────────

def test_an_agent_report_is_shown_not_spoken(main):
    host, _sink, spoken = _host(main)

    result = host._agent_progress_sink("  Writing main.py...  ")

    assert result is None
    assert spoken == [], "an execution report must never be spoken"
    assert host.ui.coding == [("stage", {"stage": "Writing main.py..."})], (
        "the real progress still reaches the live coding card")
    assert host.ui.logs == ["CODING: Writing main.py..."], (
        "and the activity log still records it")


def test_an_empty_agent_report_touches_nothing(main):
    host, _sink, spoken = _host(main)

    assert host._agent_progress_sink("   ") is None
    assert host._agent_progress_sink(None) is None
    assert host.ui.coding == []
    assert host.ui.logs == []
    assert spoken == []


def test_the_coding_tools_receive_the_report_sink_not_speak(main):
    assert MAIN_SRC.count("speak=self._agent_progress_sink") == 2, (
        "code_helper and dev_agent must both receive the display-only sink")
    assert ("code_helper(parameters=args, player=self.ui, speak=self.speak)"
            not in MAIN_SRC)
    assert ("dev_agent(parameters=args, player=self.ui, speak=self.speak)"
            not in MAIN_SRC)


# ── wiring that must survive ────────────────────────────────────────────────

def test_the_streamed_reply_goes_through_the_gate():
    assert 'self._speak_stream(new, kind="reply")' in MAIN_SRC
    assert "sink.feed(new)" not in MAIN_SRC, (
        "the streamed reply must not bypass the firewall with a direct feed")


def test_the_piper_feed_entry_point_is_kept():
    # tests/hybrid_voice/test_piper_pipeline.py asserts this literal string: the
    # firewall guards the branch, it does not replace the call it guards.
    assert "self._piper.feed(txt)" in MAIN_SRC
    assert 'self._speech_allowed("reply")' in MAIN_SRC


def test_narration_goes_through_the_speak_line_firewall():
    assert ('self._speak_line(text, kind="narration", force=forced,' in MAIN_SRC)


def test_the_system_prompt_forbids_reading_display_content_aloud():
    prompt = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    assert ("Previews, execution reports, code and page content are shown in the "
            "Chat Log.") in prompt
    assert "never read their contents aloud" in prompt


# ── the code-preview card (code_helper snippets) ────────────────────────────
#
# ``code_helper`` answers with "<status>\n\nPreview:\n<code>". The helper that
# turns that into a real Chat Log card existed but was NEVER called: the snippet
# never reached a card and the model was free to read it out. These pin the
# wiring and the firewall behavior of the helper itself.

class _CodeUI:
    """Only the display surface ``_code_preview_result`` touches."""

    def __init__(self):
        self.shown: list[tuple] = []

    def show_content(self, title, text):
        self.shown.append((str(title), str(text)))


CODE_RESULT = ("Code written. Saved to: C:/tmp/main.py\n\n"
               "Preview:\nprint('hi')\n")


def test_a_code_preview_is_shown_as_a_card_and_tagged_silent(main):
    ui = _CodeUI()
    tagged = main._code_preview_result(ui, "main.py", CODE_RESULT, "python")

    assert len(ui.shown) == 1, "the code preview must reach the Chat Log card"
    title, shown = ui.shown[0]
    assert title == "main.py"
    assert "```python" in shown, "the snippet must be fenced for highlighting"
    assert "print('hi')" in shown, "the real snippet must be inside the card"
    assert main._PREVIEW_SHOWN_TAG in tagged, (
        "the model-visible result must instruct: acknowledge, do not read aloud")


def test_a_code_result_without_a_preview_keeps_the_raw_result(main):
    ui = _CodeUI()
    plain = "Code written. Saved to: C:/tmp/main.py"
    assert main._code_preview_result(ui, "main.py", plain, "python") == plain
    assert ui.shown == [], "nothing to preview means no card"


def test_the_code_helper_branch_really_wires_the_preview():
    """Definition plus at least one call site — a helper that is never called
    only looks like a feature."""
    assert MAIN_SRC.count("_code_preview_result(") >= 2, (
        "the code-preview helper must be called, not merely defined")
    # It must sit in the code_helper dispatch branch, next to the tool call.
    helper_call = MAIN_SRC.index("_code_preview_result(\n")
    assert "code_helper(parameters=args" in MAIN_SRC[
        max(0, helper_call - 900):helper_call], (
        "the preview must be created by the code_helper branch")