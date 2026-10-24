"""Orchestrator result mapping: a recovered run is NOT a failed run.

The LangGraph path read ``errors`` (which accumulates for the whole run and is
never cleared after a successful replan) and declared a run FAILED whenever the
plan finished with a non-empty error list — so a job that hit one transient
failure, replanned, and completed every step was reported as failed. The
deterministic path already returned Completed; the graph path now matches.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import graph as g  # noqa: E402


class _StubGraph:
    """A compiled-graph stand-in: returns a fixed final state."""

    def __init__(self, final: dict):
        self._final = final

    def invoke(self, state, config=None):   # noqa: ARG002
        return self._final


def _orchestrator(tmp_path) -> g.Orchestrator:
    return g.Orchestrator(checkpointer=g.Checkpointer(tmp_path / "orch.db"))


def test_a_recovered_run_reports_completed(tmp_path):
    final = {
        "plan": ["open the app", "load the file"], "cursor": 2,
        "results": ["open the app", "load the file"],
        "errors": ["step failed: open the app — the app was still starting"],
        "replans": 1, "terminal_error": "",
        "observation": {}, "verified": True,
    }
    res = _orchestrator(tmp_path)._run_langgraph(
        "t-recovered", "open and load", {"plan": final["plan"]}, _StubGraph(final))

    assert res.ok is True, "a fully-recovered run must not be reported as failed"
    assert res.state == "Completed"
    assert res.steps_done == ["open the app", "load the file"]
    # The recovery evidence is still carried — it is not hidden.
    assert res.errors == ["step failed: open the app — the app was still starting"]


def test_an_unrecoverable_run_reports_failed(tmp_path):
    final = {
        "plan": ["do the impossible"], "cursor": 1, "results": [],
        "errors": ["step failed: do the impossible — no such engine"],
        "replans": 1, "terminal_error": "unrecoverable step: no such engine",
        "observation": {}, "verified": False,
    }
    res = _orchestrator(tmp_path)._run_langgraph(
        "t-failed", "goal", {"plan": final["plan"]}, _StubGraph(final))

    assert res.ok is False
    assert res.state == "Failed"
    assert "no such engine" in res.summary


def test_a_spent_replan_budget_reports_failed(tmp_path):
    final = {
        "plan": ["loop step"], "cursor": 0, "results": [],
        "errors": ["step failed: loop step — still failing"],
        "replans": g.MAX_REPLANS + 1, "terminal_error": "",
        "observation": {}, "verified": False,
    }
    res = _orchestrator(tmp_path)._run_langgraph(
        "t-budget", "goal", {"plan": final["plan"]}, _StubGraph(final))

    assert res.ok is False and res.state == "Failed"
