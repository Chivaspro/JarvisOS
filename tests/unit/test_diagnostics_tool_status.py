"""The dispatcher must never grade a diagnostics *report* by its wording.

Root cause this pins (reproduced before the fix): ``main._tool_result_status``
classified a tool's natural-language result by substring. A diagnostics report
legitimately contains sentences like ``coding_agents: DEGRADED — aider not
installed`` and ``fix: The model files could not be loaded.``, which matched the
``" not installed"`` / ``" could not "`` failure triggers. Every coding or
debugging task routes through the Diagnostics verification step
(brain/skills.py: chains=("debugging", "system_engineering", "verification")),
so a *successful* diagnostic marked its own task step failed, wrote an
"## Errors" section and recorded a failure against the diagnostics skill.

The fix: a report is a document ABOUT the machine. When the tool is
``diagnostics`` and the report carries the engine's own machine-readable
``VERDICT: ok|attention|failed`` line, that verdict decides — and nothing else.
FAILED then means only "a check could not be executed".

Nothing here weakens the other tools: every non-diagnostics result keeps the
original keyword/marker classification, byte for byte.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "main_impl_diag_tool_status_tests", ROOT / "main.py")
main = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = main
_spec.loader.exec_module(main)

from observability import diagnostics  # noqa: E402


#: A faithful excerpt of a real report: a healthy machine that is missing
#: optional extras. Every one of these lines used to flip the whole call to
#: FAILED on its own.
REPORT_BODY = "\n".join([
    "DIAGNOSTICS  //  ONLINE 14 · DEGRADED 7 · OFFLINE 1 · DISABLED 2",
    "- configuration: ONLINE — 103 keys, schema OK",
    "- vision: DEGRADED — MediaPipe is missing; face, hand and gesture "
    "tracking are unavailable",
    "    fix: pip install mediapipe.",
    "- coding_agents: DEGRADED — aider not installed; focused edits unavailable "
    "(pip install aider-chat)",
    "- laya_decision_ai: DEGRADED — enabled but NOT active",
    "    fix: The model files could not be loaded.",
    "- voice: DEGRADED — engine=fish_audio has not been built yet this session",
    "- agent_reach: OFFLINE — Agent Reach environment not found next to the project",
    "- tools: ONLINE — 20/30 present (10 optional integrations missing)",
])


def _report(verdict_word: str) -> str:
    """The report with the engine's own verdict vocabulary."""
    return f"{REPORT_BODY}\nVERDICT: {verdict_word}"


# ── the fix ──────────────────────────────────────────────────────────────────

def test_a_degraded_but_successful_report_is_not_failed():
    """The reported bug: healthy machine + missing optional tool == SUCCESS."""
    assert main._tool_result_status(_report("attention"),
                                    tool="diagnostics") == "SUCCESS"


def test_an_all_green_report_is_success():
    assert main._tool_result_status(_report("ok"), tool="diagnostics") == "SUCCESS"


def test_a_report_whose_check_could_not_run_is_failed():
    """ERROR is the only thing that may fail the call."""
    failed = "\n".join([
        "DIAGNOSTICS  //  ONLINE 1 · ERROR 1",
        "- tools: ERROR — check timed out after 6s",
        "    fix: A hanging probe is a bug worth reporting.",
        "VERDICT: failed",
    ])
    assert main._tool_result_status(failed, tool="diagnostics") == "FAILED"


def test_the_verdict_is_the_only_thing_trusted_for_diagnostics():
    """Failure wording without a failed verdict must NOT fail the call."""
    text = ("DIAGNOSTICS  //  ONLINE 2\n"
            "- memory: DEGRADED — the brain store could not be opened\n"
            "- coding_agents: DEGRADED — aider not installed\n"
            "VERDICT: attention")
    assert main._tool_result_status(text, tool="diagnostics") == "SUCCESS"


def test_a_diagnostics_report_without_a_verdict_is_still_scanned():
    """Older/other diagnostics output keeps the conservative keyword path."""
    assert main._tool_result_status("Diagnostics failed: ValueError: boom",
                                    tool="diagnostics") == "FAILED"
    assert main._tool_result_status("everything is fine, sir",
                                    tool="diagnostics") == "SUCCESS"


def test_the_performance_verdict_vocabulary_is_not_mistaken_for_diagnostics():
    """observability/performance.py uses the same prefix with other words."""
    assert main._diagnostics_verdict("PERFORMANCE\nVERDICT: 12 findings, 3 critical") == ""
    assert main._diagnostics_verdict(_report("attention")) == "attention"
    # And an unknown-word verdict line must not become a fake pass/fail either.
    assert main._tool_result_status("VERDICT: 12 findings, 3 critical",
                                    tool="performance_scan") == "SUCCESS"


def test_the_last_verdict_line_wins():
    text = "VERDICT: failed\n…retried…\nVERDICT: ok"
    assert main._diagnostics_verdict(text) == "ok"


def test_the_engine_and_the_classifier_agree_on_the_vocabulary():
    """Built from the real engine helpers, not a hand-copied spelling."""
    healthy = [diagnostics.Check("tools", diagnostics.ONLINE, "ok")]
    degraded = [diagnostics.Check("tools", diagnostics.DEGRADED, "missing"),
                diagnostics.Check("wake_word", diagnostics.DISABLED, "off")]
    broken = [diagnostics.Check("tools", diagnostics.ERROR, "timed out")]
    for checks, expected in ((healthy, "SUCCESS"), (degraded, "SUCCESS"),
                             (broken, "FAILED")):
        line = diagnostics.verdict_line(checks)
        assert main._tool_result_status(line, tool="diagnostics") == expected, line


# ── no regression for every other tool ───────────────────────────────────────

def test_other_tools_keep_the_keyword_classification():
    assert main._tool_result_status("aider not installed",
                                    tool="code_helper") == "FAILED"
    assert main._tool_result_status("The file could not be written",
                                    tool="file_controller") == "FAILED"
    assert main._tool_result_status("failed to open the page",
                                    tool="browser_control") == "FAILED"
    assert main._tool_result_status("Opened the JARVIS WebView.",
                                    tool="computer_control") == "SUCCESS"


def test_capability_markers_still_outrank_everything():
    assert main._tool_result_status("status=not_done", tool="x") == "FAILED"
    assert main._tool_result_status("verified=false", tool="x") == "VERIFICATION_FAILED"
    assert main._tool_result_status("[unverified] did the thing", tool="x") == \
        "VERIFICATION_FAILED"
    # Even for diagnostics: the router's own markers are explicit outcomes.
    assert main._tool_result_status("status=not_done\nVERDICT: ok",
                                    tool="diagnostics") == "FAILED"


def test_an_empty_diagnostics_result_is_not_a_pass_by_verdict():
    assert main._tool_result_status("", tool="diagnostics") == "SUCCESS"


def test_permission_refusal_is_unchanged():
    assert main._tool_result_status(_report("attention"), True,
                                    "diagnostics") == "REQUIRES_PERMISSION"


# ── the orchestrator path (the second caller that was failing) ───────────────

class _Host:
    """Just enough object for the unbound method: it uses no instance state."""


def _step(monkeypatch, text: str):
    monkeypatch.setattr(diagnostics, "handle_diagnostics",
                        lambda args: text, raising=True)
    host = _Host()
    call = main.JarvisLive._run_orchestrator_step.__get__(host)
    return call({"tool": "diagnostics", "args": {"mode": "all"}}, {})


def test_orchestrator_step_for_diagnostics_is_ok_and_verified(monkeypatch):
    obs = _step(monkeypatch, _report("attention"))
    assert obs["ok"] is True, obs
    assert obs["verified"] is True, obs
    assert obs["error"] == "", obs


def test_orchestrator_step_still_fails_a_real_diagnostics_error(monkeypatch):
    obs = _step(monkeypatch, "DIAGNOSTICS\n- tools: ERROR — timed out\n"
                             "VERDICT: failed")
    assert obs["ok"] is False, obs
    assert obs["verified"] is False, obs
