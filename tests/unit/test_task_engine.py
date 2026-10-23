"""Task engine — lifecycle, honest steps, isolation, persistence, reports.

The behaviours pinned here are the ones the feature promises in production:
a step completes only when the real operation succeeded, failed/cancelled work
keeps its partial results, events never cross between tasks, and the report
carries real execution data with secrets scrubbed.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from brain import task_engine
from brain.task_engine import (
    TaskRegistry, TASK_STATES, TERMINAL_STATES, stage_labels,
)


@pytest.fixture()
def registry(tmp_path):
    return TaskRegistry(
        db_path=tmp_path / "task_records.sqlite",
        reports_dir=tmp_path / "reports",
        emit_logs=False,
    )


def _begin(reg, **kw):
    return reg.begin_request(kw.pop("title", "Research quantum computing"),
                             **kw)


# ── creation & lifecycle ──────────────────────────────────────────────────────

def test_begin_request_creates_record_with_stage_steps(registry):
    tid = _begin(registry, source="text", category="SEARCH", request="research X")
    rec = registry.get(tid)
    assert rec is not None
    assert rec.id.startswith("TASK-")
    assert rec.state == "CREATED"
    assert rec.title == "Research quantum computing"
    assert [s.title for s in rec.steps] == [
        stage_labels("SEARCH")[0], "Deliver response",
    ]
    assert rec.questions_state == "pending"


def test_lifecycle_follows_created_initializing_running_validating_completed(registry):
    tid = _begin(registry)
    registry.note_analysis(tid, detail="no clarification needed")
    assert registry.get(tid).state == "INITIALIZING"
    registry.mark_running(tid)
    assert registry.get(tid).state == "RUNNING"
    registry.mark_validating(tid)
    assert registry.get(tid).state == "VALIDATING"
    registry.complete(tid, summary="done")
    assert registry.get(tid).state == "COMPLETED"


def test_invalid_transition_is_refused_silently(registry):
    tid = _begin(registry)
    registry.mark_running(tid)
    assert registry.get(tid).state == "RUNNING"
    # A late analysis note cannot drag running work backwards.
    registry.note_analysis(tid, detail="late")
    assert registry.get(tid).state == "RUNNING"
    registry.complete(tid)
    assert registry.get(tid).state == "COMPLETED"
    # A terminal state never moves again.
    registry.fail(tid, "late failure")
    assert registry.get(tid).state == "COMPLETED"


# ── tool steps & the honesty contract ─────────────────────────────────────────

def test_tool_step_is_inserted_before_validation_stage(registry):
    tid = _begin(registry, category="SEARCH")
    registry.note_analysis(tid)
    registry.note_tool_started(tid, "web_search", detail="quantum", corr="c1")
    rec = registry.get(tid)
    order = [s.id for s in rec.steps]
    assert order[0] == "analyze"
    assert order[-1] == "deliver"
    assert "validate" in order
    tool_index = next(i for i, s in enumerate(rec.steps) if s.metadata.get("tool"))
    assert tool_index < order.index("validate"), "real work precedes validation"
    assert rec.state == "RUNNING"


def test_step_completes_only_on_real_success(registry):
    tid = _begin(registry)
    registry.note_tool_started(tid, "web_search", detail="q", corr="c1")
    registry.note_tool_result(tid, "c1", "FAILED", excerpt="connection error")
    rec = registry.get(tid)
    tool = next(s for s in rec.steps if s.metadata.get("tool"))
    assert tool.status == "failed"
    assert "connection error" in tool.outcome
    assert rec.errors  # the failure is recorded as evidence


def test_success_completes_the_step_and_records_a_finding(registry):
    tid = _begin(registry)
    registry.note_tool_started(tid, "file_processor", detail="a.pdf", corr="c2")
    registry.note_tool_result(tid, "c2", "SUCCESS", excerpt="extracted 12 pages",
                              artifacts=["C:/out/a.txt"])
    rec = registry.get(tid)
    tool = next(s for s in rec.steps if s.metadata.get("tool"))
    assert tool.status == "completed"
    assert any("extracted 12 pages" in f for f in rec.findings)
    assert "C:/out/a.txt" in rec.artifacts


def test_permission_refusal_skips_the_step_not_fails_it(registry):
    tid = _begin(registry)
    registry.note_tool_started(tid, "computer_control", detail="open app", corr="c3")
    registry.note_tool_result(tid, "c3", "REQUIRES_PERMISSION",
                              excerpt="reminder automation is switched off")
    rec = registry.get(tid)
    tool = next(s for s in rec.steps if s.metadata.get("tool"))
    assert tool.status == "skipped"
    assert rec.warnings
    assert not rec.errors


def test_complete_skips_steps_whose_work_never_ran(registry):
    tid = _begin(registry, category="CODE")
    # No analysis, no tools — a bare completion must not invent successes.
    registry.complete(tid, summary="answered")
    rec = registry.get(tid)
    assert rec.stage("analyze").status == "skipped"
    assert rec.stage("deliver").status == "completed"


def test_running_step_is_failed_on_task_failure(registry):
    tid = _begin(registry)
    registry.note_tool_started(tid, "dev_agent", detail="build", corr="c4")
    registry.fail(tid, "toolchain missing")
    rec = registry.get(tid)
    tool = next(s for s in rec.steps if s.metadata.get("tool"))
    assert tool.status == "failed"
    assert rec.state == "FAILED"
    assert "toolchain missing" in "\n".join(rec.errors)


def test_answer_only_task_uses_lightweight_checklist(registry):
    tid = registry.begin_request("What is quantum entanglement?", category="CHAT")
    registry.note_analysis(tid, detail="clear request")
    registry.complete(tid, summary="explained")
    rec = registry.get(tid)
    assert rec.state == "COMPLETED"
    assert not any(s.metadata.get("tool") for s in rec.steps)
    assert rec.stage("deliver").status == "completed"


# ── questions ─────────────────────────────────────────────────────────────────

def test_questions_waiting_and_resolved_mark_a_real_step(registry):
    tid = _begin(registry, category="CODE")
    registry.note_questions_waiting(tid, ["approach", "files"])
    step = registry.get(tid).stage("Answer clarification questions")
    assert step.status == "waiting"
    assert step.metadata["question_ids"] == ["approach", "files"]
    registry.note_questions_resolved(tid, summary="approach=reuse")
    assert registry.get(tid).stage("Answer clarification questions").status == "completed"
    assert registry.get(tid).questions_state == "answered"


def test_cancelled_question_flow_is_skipped_not_completed(registry):
    tid = _begin(registry)
    registry.note_questions_waiting(tid, ["approach"])
    registry.note_questions_resolved(tid, cancelled=True)
    step = registry.get(tid).stage("Answer clarification questions")
    assert step.status == "skipped"
    assert registry.get(tid).questions_state == "cancelled"


def test_answers_are_stored_against_the_task(registry):
    tid = _begin(registry)
    registry.set_answers(tid, {"approach": "reuse", "files": ["a.py", "b.py"]})
    assert registry.get(tid).answers["approach"] == "reuse"


# ── cancellation & partial results ────────────────────────────────────────────

def test_cancel_preserves_partial_results(registry):
    tid = _begin(registry)
    registry.note_tool_started(tid, "web_search", detail="q", corr="c5")
    registry.note_tool_result(tid, "c5", "SUCCESS", excerpt="found 3 sources")
    registry.note_tool_started(tid, "web_research", detail="deep", corr="c6")
    registry.cancel(tid, "user interrupted")
    rec = registry.get(tid)
    assert rec.state == "CANCELLED"
    assert any("found 3 sources" in f for f in rec.findings)
    assert any("user interrupted" in w for w in rec.warnings)
    open_steps = [s for s in rec.steps if s.status == "running"]
    assert not open_steps


# ── isolation & events ────────────────────────────────────────────────────────

def test_multiple_tasks_never_cross_talk(registry):
    events: list[dict] = []
    registry.add_listener(events.append)
    a = _begin(registry, title="Task A")
    b = _begin(registry, title="Task B")
    registry.note_analysis(a, detail="A analyzed")
    registry.note_tool_started(a, "web_search", detail="A", corr="ca")
    registry.note_tool_result(a, "ca", "SUCCESS", excerpt="A result")
    registry.note_tool_started(b, "file_processor", detail="B", corr="cb")
    registry.note_tool_result(b, "cb", "FAILED", excerpt="B error")
    registry.complete(a, summary="A done")
    registry.cancel(b, "B dropped")

    rec_a, rec_b = registry.get(a), registry.get(b)
    assert rec_a.state == "COMPLETED" and rec_b.state == "CANCELLED"
    assert not rec_a.errors and rec_b.errors
    assert rec_a.title == "Task A" and rec_b.title == "Task B"
    # Every event belongs to exactly one of the two tasks.
    assert {e["id"] for e in events} == {a, b}
    a_events = [e for e in events if e["id"] == a]
    assert all(e["title"] == "Task A" for e in a_events)


def test_a_broken_listener_cannot_break_the_engine(registry):
    def boom(_payload):
        raise RuntimeError("listener exploded")

    registry.add_listener(boom)
    tid = _begin(registry)          # must not raise
    registry.complete(tid)
    assert registry.get(tid).state == "COMPLETED"


def test_active_id_returns_most_recent_open_task(registry):
    a = _begin(registry, title="A")
    registry.complete(a)
    b = _begin(registry, title="B")
    assert registry.active_id() == b
    registry.cancel(b)
    assert registry.active_id() == ""


# ── persistence ───────────────────────────────────────────────────────────────

def test_record_is_persisted_to_sqlite(registry, tmp_path):
    tid = _begin(registry, category="CODE")
    registry.complete(tid, summary="done")
    db = tmp_path / "task_records.sqlite"
    assert db.exists()
    with sqlite3.connect(db) as con:
        rows = con.execute(
            "SELECT task_id, state, payload FROM task_records"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == tid
    assert rows[0][1] == "COMPLETED"
    payload = json.loads(rows[0][2])
    assert payload["title"] == "Research quantum computing"


def test_store_is_pruned_to_the_newest_records(tmp_path):
    reg = TaskRegistry(db_path=tmp_path / "t.sqlite",
                       reports_dir=tmp_path / "r", emit_logs=False)
    for i in range(task_engine._MAX_RECORDS + 12):
        reg.begin_request(f"task {i}")
    assert len(reg.list_tasks(limit=10_000)) <= task_engine._MAX_RECORDS


# ── reports & redaction ───────────────────────────────────────────────────────

def test_report_contains_the_required_sections(registry, tmp_path):
    tid = _begin(registry, category="SEARCH", request="research fusion energy")
    registry.note_analysis(tid, detail="no clarification needed")
    registry.note_tool_started(tid, "web_search", detail="fusion", corr="c7")
    registry.note_tool_result(tid, "c7", "SUCCESS", excerpt="found 4 sources",
                              artifacts=["data/out/report.md"])
    registry.set_answers(tid, {"depth": "deep"})
    registry.note_warning(tid, "one source was unreachable")
    registry.complete(tid, summary="summarised four sources")
    text = registry.report_text(tid)
    for needle in ("Task report", tid, "COMPLETED", "Timeline", "Elapsed",
                   "Checklist", "Decisions that shaped the plan",
                   "Findings and results", "Generated files", "Warnings",
                   "Validation"):
        assert needle in text, needle
    path = registry.report_path(tid)
    assert path and (tmp_path / "reports" / f"{tid}.md").exists()


def test_failed_and_cancelled_tasks_still_produce_reports(registry, tmp_path):
    a = _begin(registry, title="Will fail")
    registry.fail(a, "no engine available")
    b = _begin(registry, title="Will cancel")
    registry.cancel(b, "user stopped it")
    for tid in (a, b):
        assert (tmp_path / "reports" / f"{tid}.md").exists()
        assert registry.report_path(tid)
    assert "no engine available" in registry.report_text(a)
    assert "user stopped it" in registry.report_text(b)


def test_reports_and_events_never_leak_secret_shaped_tokens(registry):
    secret = "AIzaSy" + "A" * 33
    tid = _begin(registry)
    registry.note_finding(tid, f"key was {secret}")
    registry.complete(tid, summary=f"used {secret}")
    text = registry.report_text(tid)
    assert secret not in text
    assert "[REDACTED]" in text


def test_elapsed_time_is_measured_from_creation(registry):
    tid = _begin(registry)
    registry.complete(tid)
    assert registry.get(tid).elapsed_ms() >= 0


def test_stage_labels_fall_back_for_unknown_categories():
    assert stage_labels("SEARCH")[0].startswith("Analyze")
    assert stage_labels("nonsense")[0] == "Analyze the request"


def test_lifecycle_vocabulary_is_complete():
    assert TASK_STATES == ("CREATED", "INITIALIZING", "RUNNING", "VALIDATING",
                           "COMPLETED", "FAILED", "CANCELLED")
    assert TERMINAL_STATES == {"COMPLETED", "FAILED", "CANCELLED"}
