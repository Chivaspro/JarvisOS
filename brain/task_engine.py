"""Automatic task teardown — one execution record per supported operation.

WHY THIS MODULE EXISTS
    J.A.R.V.I.S. already had two pieces of task machinery and neither answered
    the question "what did you actually do, step by step, and did it work?":

    * ``brain/tasks.py`` is a **model-driven** facade over the memory database
      — the model must *choose* to call the ``task_plan`` tool, so ordinary
      work produced no record at all;
    * ``brain/graph.py``'s Orchestrator emits task-state events, but only when
      the optional LangGraph path is switched on, and nothing rendered them.

    This module is the missing **writer**: the live dispatcher (``main.py``)
    reports real execution events here, and every supported operation gets its
    own record with a unique id, its own lifecycle and an honest checklist.

LIFECYCLE
    ``CREATED → INITIALIZING → RUNNING → VALIDATING → COMPLETED``
    plus the two failure terminals ``FAILED`` and ``CANCELLED``.

HONESTY CONTRACT (the reason the code looks the way it does)
    * a step completes **only** when its real operation reported success —
      a tool returning without raising is not success, the dispatcher's own
      classification is;
    * a planned step whose work never actually happened is marked ``skipped``,
      never ``completed``;
    * findings, artifacts and errors carry the real result text, never an
      invented metric;
    * persistence and report writing are best-effort: a broken disk degrades to
      an in-memory record (``degrade, never crash``), it never raises into the
      dispatcher.

Zero heavy imports; thread-safe; every public method is guarded.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "TaskStep",
    "TaskRecord",
    "TaskRegistry",
    "get_registry",
    "reset_registry",
    "new_task_id",
    "TASK_STATES",
    "TERMINAL_STATES",
    "STEP_STATES",
    "TASK_EVENTS",
    "stage_labels",
]

# ── lifecycle vocabulary ──────────────────────────────────────────────────────

TASK_STATES: tuple[str, ...] = (
    "CREATED", "INITIALIZING", "RUNNING", "VALIDATING",
    "COMPLETED", "FAILED", "CANCELLED",
)
TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

# Allowed forward transitions for one record. A terminal state never moves.
# A trivial request may legitimately skip straight from CREATED to a later
# state (a pure answer that never initialises or runs a tool).
_ALLOWED: dict[str, set[str]] = {
    "CREATED": {"INITIALIZING", "RUNNING", "VALIDATING", "COMPLETED",
                "FAILED", "CANCELLED"},
    "INITIALIZING": {"RUNNING", "VALIDATING", "COMPLETED", "FAILED", "CANCELLED"},
    "RUNNING": {"VALIDATING", "COMPLETED", "FAILED", "CANCELLED"},
    "VALIDATING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}

STEP_STATES: tuple[str, ...] = (
    "pending", "waiting", "running", "completed", "skipped", "failed",
)
_OPEN_STEP_STATES = frozenset({"pending", "waiting", "running"})

TASK_EVENTS: tuple[str, ...] = (
    "task_created", "task_started", "step_started", "step_completed",
    "step_skipped", "progress", "questions_waiting", "questions_resolved",
    "task_completed", "task_failed", "task_cancelled",
)

# ── per-operation stage labels ────────────────────────────────────────────────
# The *stages* are only labels for the two lifecycle steps every task has
# (analysis + validation). The real work items are the per-tool steps the
# dispatcher reports, titled from the live activity detail — so a research run
# shows "SEARCH · <query>" and a coding run shows "CODE · <target>", which is
# the operation that actually happened.
_STAGES: dict[str, tuple[str, str]] = {
    "SEARCH":   ("Analyze the research request", "Validate the sources and findings"),
    "FIND":     ("Analyze the request", "Validate the results"),
    "CODE":     ("Analyze the coding request", "Verify the change"),
    "REVIEW":   ("Analyze the review request", "Validate the conclusions"),
    "ANALYZE":  ("Analyze the request", "Validate the output"),
    "GENERATE": ("Analyze the generation request", "Verify the generated artifact"),
    "AUTOMATE": ("Analyze the control request", "Verify the action result"),
    "VIEW":     ("Analyze the request", "Validate what was viewed"),
    "CHAT":     ("Understand the request", "Deliver the response"),
}
_DEFAULT_STAGE = ("Analyze the request", "Validate the results")


def stage_labels(category: str) -> tuple[str, str]:
    """(analysis_label, validation_label) for an activity category."""
    return _STAGES.get(str(category or "").upper(), _DEFAULT_STAGE)


# ── secret scrubbing (one source of truth: the logger's own scrubber) ─────────

_SECRET_PATTERN = re.compile(
    r"\b(AIzaSy[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})\b"
)


def _scrub(text: Any) -> str:
    """Redact secret-shaped tokens from a string.

    Prefers the structured logger's scrubber so there is exactly one pattern in
    the codebase; falls back to the identical local pattern when the logger is
    unavailable (a headless test process).
    """
    out = str(text if text is not None else "")
    try:
        from observability.logger import _scrub_text as _fn  # noqa: PLC0415

        return _fn(out)
    except Exception:
        return _SECRET_PATTERN.sub("[REDACTED]", out)


def _now() -> float:
    return time.time()


def new_task_id() -> str:
    """Unique, persisted, human-readable task id.

    Reuses the diagnostics correlation generator (``TASK-2026-000001``), which
    persists its counter so an id is never reused across restarts. Falls back to
    a random id when the observability layer is unavailable.
    """
    try:
        from observability.diagnostics import new_correlation  # noqa: PLC0415

        return str(new_correlation("TASK"))
    except Exception:
        return f"TASK-{time.strftime('%Y')}-{uuid.uuid4().hex[:8]}"


# ── models ────────────────────────────────────────────────────────────────────

@dataclass
class TaskStep:
    """One checklist item with a real, observable outcome."""

    id: str
    title: str
    status: str = "pending"
    description: str = ""
    outcome: str = ""
    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def open(self) -> bool:
        return self.status in _OPEN_STEP_STATES

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": _scrub(self.title),
            "status": self.status,
            "description": _scrub(self.description),
            "outcome": _scrub(self.outcome),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": {k: _scrub(v) for k, v in (self.metadata or {}).items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskStep":
        data = dict(data or {})
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            title=str(data.get("title") or ""),
            status=str(data.get("status") or "pending"),
            description=str(data.get("description") or ""),
            outcome=str(data.get("outcome") or ""),
            created_at=float(data.get("created_at") or _now()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class TaskRecord:
    """One user operation: what was asked, what ran, and how it ended."""

    id: str
    title: str
    request: str = ""
    source: str = "text"            # text | voice | provider | orchestrator
    category: str = ""              # SEARCH / CODE / … (activity vocabulary)
    state: str = "CREATED"
    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None
    steps: list[TaskStep] = field(default_factory=list)
    answers: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    plan_id: str = ""
    questions_state: str = "pending"   # pending | none_needed | answered | cancelled
    cancellable: bool = True
    summary: str = ""

    # ── small helpers ─────────────────────────────────────────────────────────
    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def elapsed_ms(self) -> int:
        end = self.finished_at or _now()
        return max(0, int((end - self.created_at) * 1000))

    def step(self, step_id: str) -> TaskStep | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def stage(self, label: str) -> TaskStep | None:
        """Find a step by its stable id *or* its title.

        Callers use both: the lifecycle stages are addressed by id
        (``"analyze"``/``"validate"``/``"deliver"``) while the question step is
        addressed by its title.
        """
        key = str(label or "")
        for s in self.steps:
            if s.id == key or s.title == key:
                return s
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": _scrub(self.title),
            "request": _scrub(self.request),
            "source": self.source,
            "category": self.category,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms(),
            "steps": [s.to_dict() for s in self.steps],
            "answers": {k: _scrub(v) for k, v in (self.answers or {}).items()},
            "findings": [_scrub(x) for x in self.findings],
            "warnings": [_scrub(x) for x in self.warnings],
            "errors": [_scrub(x) for x in self.errors],
            "artifacts": [_scrub(x) for x in self.artifacts],
            "plan_id": self.plan_id,
            "questions_state": self.questions_state,
            "cancellable": bool(self.cancellable and not self.terminal),
            "summary": _scrub(self.summary),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        data = dict(data or {})
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            request=str(data.get("request") or ""),
            source=str(data.get("source") or "text"),
            category=str(data.get("category") or ""),
            state=str(data.get("state") or "CREATED"),
            created_at=float(data.get("created_at") or _now()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            steps=[TaskStep.from_dict(s) for s in (data.get("steps") or [])],
            answers=dict(data.get("answers") or {}),
            findings=[str(x) for x in (data.get("findings") or [])],
            warnings=[str(x) for x in (data.get("warnings") or [])],
            errors=[str(x) for x in (data.get("errors") or [])],
            artifacts=[str(x) for x in (data.get("artifacts") or [])],
            plan_id=str(data.get("plan_id") or ""),
            questions_state=str(data.get("questions_state") or "pending"),
            cancellable=bool(data.get("cancellable", True)),
            summary=str(data.get("summary") or ""),
        )


# ── the registry ──────────────────────────────────────────────────────────────

_MAX_RECORDS = 200          # pruned on write (newest kept)
_MAX_TEXT = 600             # per free-text field, keeps records small


def _clip(text: Any, limit: int = _MAX_TEXT) -> str:
    return _scrub(str(text if text is not None else ""))[:limit]


class TaskRegistry:
    """Thread-safe task store + event bus + report writer (never raises)."""

    def __init__(self, db_path: str | Path | None = None,
                 reports_dir: str | Path | None = None,
                 emit_logs: bool = True) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, TaskRecord] = {}
        self._order: list[str] = []          # insertion order, oldest first
        self._listeners: list[Callable[[dict], None]] = []
        self._emit_logs = bool(emit_logs)
        default_db, default_reports = self._default_paths()
        self._db_path = Path(db_path) if db_path else default_db
        self._reports_dir = Path(reports_dir) if reports_dir else default_reports
        self._db_ready = False

    # ── wiring ────────────────────────────────────────────────────────────────
    @staticmethod
    def _default_paths() -> tuple[Path, Path]:
        try:
            from config.settings import DATA_DIR  # noqa: PLC0415

            base = Path(DATA_DIR)
            return (base / "runtime" / "task_records.sqlite", base / "reports")
        except Exception:
            return (Path("data") / "runtime" / "task_records.sqlite",
                    Path("data") / "reports")

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            if callable(fn) and fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def reset(self) -> None:
        """Test/teardown hook: drop in-memory state (leaves the DB alone)."""
        with self._lock:
            self._records.clear()
            self._order.clear()

    # ── reads ─────────────────────────────────────────────────────────────────
    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._records.get(str(task_id or ""))

    def active_id(self) -> str:
        """Most recent non-terminal task id (empty when none)."""
        with self._lock:
            for tid in reversed(self._order):
                rec = self._records.get(tid)
                if rec is not None and not rec.terminal:
                    return tid
        return ""

    def snapshot(self, task_id: str) -> dict:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None:
                return {}
            return self._snapshot(rec)

    def list_tasks(self, *, include_terminal: bool = True,
                   limit: int = 25) -> list[dict]:
        out: list[dict] = []
        with self._lock:
            for tid in reversed(self._order):
                rec = self._records.get(tid)
                if rec is None:
                    continue
                if not include_terminal and rec.terminal:
                    continue
                out.append(self._snapshot(rec))
                if len(out) >= max(1, int(limit)):
                    break
        return out

    # ── lifecycle API (called by the dispatcher) ──────────────────────────────
    def begin_request(self, title: str, *, source: str = "text",
                      category: str = "", request: str = "") -> str:
        """Open a record for one user request. Returns the task id."""
        tid = new_task_id()
        cat = str(category or "").upper()
        analysis_label, _validation = stage_labels(cat)
        rec = TaskRecord(
            id=tid,
            title=_clip(title, 160) or "Untitled task",
            request=_clip(request or title, 400),
            source=str(source or "text"),
            category=cat,
            state="CREATED",
        )
        rec.steps.append(TaskStep(id="analyze", title=analysis_label))
        rec.steps.append(TaskStep(id="deliver", title="Deliver response"))
        with self._lock:
            self._records[tid] = rec
            self._order.append(tid)
            self._prune_locked()
        self._emit(rec, "task_created", detail=rec.title)
        self._save(rec)
        return tid

    def note_analysis(self, task_id: str, summary: str = "",
                      detail: str = "") -> None:
        """The analysis / clarification stage resolved.

        ``detail`` is the real outcome (e.g. "no clarification needed" or the
        confirmed decisions), recorded as the step's outcome.
        """
        self._transition(task_id, "INITIALIZING")
        self._finish_stage(task_id, "analyze", outcome=detail or "analyzed")
        if summary:
            self._append(task_id, "findings", summary)

    def mark_running(self, task_id: str) -> None:
        self._transition(task_id, "RUNNING")
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is not None and rec.started_at is None:
                rec.started_at = _now()
                self._save(rec)

    def note_tool_started(self, task_id: str, tool: str, detail: str = "",
                          corr: str = "", metadata: dict | None = None) -> str:
        """Create + start a checklist step for one real tool dispatch.

        Returns the step id (empty when the task is unknown/terminal).
        """
        title = f"{str(tool or 'tool')}"
        if detail:
            title = f"{title} · {detail}"
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return ""
            if rec.stage("validate") is None:
                _a, validation_label = stage_labels(rec.category)
                # The validation stage sits after the real work: keep it just
                # before the trailing "deliver response" step.
                rec.steps.insert(max(1, len(rec.steps) - 1),
                                 TaskStep(id="validate", title=validation_label))
            step = TaskStep(
                id=uuid.uuid4().hex[:8],
                title=_clip(title, 160),
                status="running",
                started_at=_now(),
                description=_clip(detail, 200),
                metadata={"tool": str(tool or ""), "corr": str(corr or ""),
                          **(metadata or {})},
            )
            # Real work items land BEFORE the validation stage, in the order
            # they actually ran; with no validation stage yet, before deliver.
            validate_step = rec.stage("validate")
            deliver_step = rec.stage("deliver")
            if validate_step is not None:
                insert_at = rec.steps.index(validate_step)
            elif deliver_step is not None:
                insert_at = rec.steps.index(deliver_step)
            else:
                insert_at = len(rec.steps)
            rec.steps.insert(insert_at, step)
            started = rec.started_at is None
            if started:
                rec.started_at = _now()
            self._set_state_locked(rec, "RUNNING")
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, "step_started", detail=step.title,
                        step_id=step.id)
        if started:
            self._emit_dict(snapshot, "task_started", detail=rec.title)
        self._save(self.get(task_id))
        return step.id

    def note_tool_result(self, task_id: str, corr: str, status: str,
                         excerpt: str = "", artifacts: Iterable[str] | None = None,
                         step_id: str = "") -> None:
        """Close the tool step with the dispatcher's REAL outcome.

        ``status`` uses the dispatcher's own vocabulary
        (``SUCCESS`` / ``FAILED`` / ``VERIFICATION_FAILED`` /
        ``REQUIRES_PERMISSION``). Only ``SUCCESS`` completes the step; permission
        refusals are marked skipped (nothing ran); everything else fails it.
        """
        status_u = str(status or "").upper()
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            step = None
            if step_id:
                step = rec.step(step_id)
            if step is None:                      # fall back: newest open tool step
                for s in reversed(rec.steps):
                    if s.status == "running" and s.metadata.get("tool"):
                        step = s
                        break
            if step is None:
                return
            step.finished_at = _now()
            if status_u == "SUCCESS":
                step.status = "completed"
                step.outcome = _clip(excerpt, 240) or "completed"
            elif status_u == "REQUIRES_PERMISSION":
                step.status = "skipped"
                step.outcome = _clip(excerpt, 240) or "permission refused"
                rec.warnings.append(f"{step.title}: permission refused")
            else:
                step.status = "failed"
                step.outcome = _clip(excerpt, 240) or status_u.lower()
                rec.errors.append(f"{step.title}: {step.outcome}")
            if excerpt and status_u == "SUCCESS" and "tool" in step.metadata:
                rec.findings.append(f"{step.metadata.get('tool')}: {_clip(excerpt, 200)}")
            for art in (artifacts or []):
                clean = _clip(art, 400)
                if clean and clean not in rec.artifacts:
                    rec.artifacts.append(clean)
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot,
                        "step_completed" if step.status == "completed" else
                        ("step_skipped" if step.status == "skipped" else "progress"),
                        detail=f"{step.title} — {step.outcome}", step_id=step.id)
        self._save(self.get(task_id))

    def note_questions_waiting(self, task_id: str, question_ids: Iterable[str] | None = None) -> None:
        """The gate is waiting for the user: visible WAITING step, no timeout."""
        qids = [str(q) for q in (question_ids or [])]
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            step = rec.stage("Answer clarification questions")
            if step is None:
                step = TaskStep(id="questions", title="Answer clarification questions")
                rec.steps.insert(max(1, len(rec.steps) - 2), step)
            step.status = "waiting"
            step.metadata["question_ids"] = qids
            rec.questions_state = "pending"
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, "questions_waiting",
                        detail=f"{len(qids)} question(s)")
        self._save(self.get(task_id))

    def note_questions_resolved(self, task_id: str, summary: str = "",
                                cancelled: bool = False) -> None:
        """The question flow finished (answered) or was dropped (cancelled)."""
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            step = rec.stage("Answer clarification questions")
            if step is not None:
                step.finished_at = _now()
                step.status = "skipped" if cancelled else "completed"
                step.outcome = _clip(summary, 240) or (
                    "clarification cancelled" if cancelled else "answered")
            if cancelled:
                rec.questions_state = "cancelled"
            else:
                rec.questions_state = "answered" if summary else "none_needed"
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, "questions_resolved",
                        detail=summary or rec.questions_state)
        self._save(self.get(task_id))

    def set_answers(self, task_id: str, answers: dict) -> None:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            for key, value in (answers or {}).items():
                rec.answers[str(key)] = value
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, "progress", detail="answers recorded")
        self._save(self.get(task_id))

    def note_finding(self, task_id: str, text: str) -> None:
        self._append(task_id, "findings", text)

    def note_warning(self, task_id: str, text: str) -> None:
        self._append(task_id, "warnings", text)

    def note_error(self, task_id: str, text: str) -> None:
        self._append(task_id, "errors", text)

    def add_artifact(self, task_id: str, path: str) -> None:
        self._append(task_id, "artifacts", path, limit=20)

    def link_plan(self, task_id: str, plan_id: Any) -> None:
        """Attach the model-driven ``task_plan`` id as metadata (linkage only)."""
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            rec.plan_id = str(plan_id or "")
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, "progress", detail=f"plan #{rec.plan_id}")

    def mark_validating(self, task_id: str, detail: str = "") -> None:
        self._transition(task_id, "VALIDATING", detail=detail)

    def complete(self, task_id: str, summary: str = "",
                 validation_detail: str = "") -> None:
        """Finish the task: validate stage + honest sweep of open steps."""
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            # An analysis stage that never actually ran is honestly skipped,
            # never retro-completed.
            self._finish_stage_locked(rec, "analyze", default_status="skipped")
            validate = rec.stage("validate")
            if validate is not None:
                validate.finished_at = _now()
                if validate.status in _OPEN_STEP_STATES:
                    validate.status = "completed"
                validate.outcome = _clip(validation_detail, 240) or "checked"
            deliver = rec.stage("deliver")
            if deliver is not None:
                deliver.finished_at = _now()
                deliver.status = "completed"
                deliver.outcome = _clip(summary, 240) or "delivered"
            # Any step whose real work never ran is honestly SKIPPED, never
            # reported as completed.
            for s in rec.steps:
                if s.open:
                    s.status = "skipped"
                    s.finished_at = _now()
                    s.outcome = s.outcome or "not performed"
            rec.summary = _clip(summary, 400)
            rec.finished_at = _now()
            self._set_state_locked(rec, "COMPLETED")
            snapshot = self._snapshot(rec)
            record = rec
        self._emit_dict(snapshot, "task_completed",
                        detail=record.summary or record.title)
        self._save(record)
        self._write_report(record)

    def fail(self, task_id: str, reason: str = "") -> None:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            note = _clip(reason, 300) or "failed"
            if note not in rec.errors:
                rec.errors.append(note)
            for s in rec.steps:
                if s.open:
                    s.status = "failed" if s.status == "running" else "skipped"
                    s.finished_at = _now()
                    s.outcome = s.outcome or note
            validate = rec.stage("validate")
            if validate is not None and validate.status in _OPEN_STEP_STATES:
                validate.status = "failed"
                validate.finished_at = _now()
                validate.outcome = note
            rec.finished_at = _now()
            self._set_state_locked(rec, "FAILED")
            snapshot = self._snapshot(rec)
            record = rec
        self._emit_dict(snapshot, "task_failed", detail=note)
        self._save(record)
        self._write_report(record)

    def cancel(self, task_id: str, reason: str = "") -> None:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            note = _clip(reason, 300) or "cancelled"
            if note not in rec.warnings:
                rec.warnings.append(note)
            for s in rec.steps:
                if s.open:
                    s.status = "skipped"
                    s.finished_at = _now()
                    s.outcome = s.outcome or note
            validate = rec.stage("validate")
            if validate is not None and validate.status in _OPEN_STEP_STATES:
                validate.status = "skipped"
                validate.finished_at = _now()
                validate.outcome = "not reached — task cancelled"
            rec.finished_at = _now()
            self._set_state_locked(rec, "CANCELLED")
            snapshot = self._snapshot(rec)
            record = rec
        self._emit_dict(snapshot, "task_cancelled", detail=note)
        self._save(record)
        self._write_report(record)

    # ── internals ─────────────────────────────────────────────────────────────
    def _snapshot(self, rec: TaskRecord) -> dict:
        data = rec.to_dict()
        data["event"] = ""
        return data

    def _emit(self, rec: TaskRecord, event: str, detail: str = "",
              step_id: str = "") -> None:
        with self._lock:
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, event, detail=detail, step_id=step_id)

    def _emit_dict(self, snapshot: dict, event: str, detail: str = "",
                   step_id: str = "") -> None:
        payload = dict(snapshot or {})
        payload["event"] = str(event)
        payload["detail"] = _scrub(detail)
        payload["step_id"] = str(step_id or "")
        payload["ts"] = _now()
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(payload)
            except Exception:
                pass
        if self._emit_logs:
            try:
                from observability.logger import log_event  # noqa: PLC0415

                log_event(
                    "INFO" if event not in ("task_failed",) else "WARNING",
                    "tasks", event,
                    task_id=str(payload.get("id") or ""),
                    state=str(payload.get("state") or ""),
                    result={"detail": payload.get("detail") or "",
                            "step_id": payload.get("step_id") or ""},
                )
            except Exception:
                pass

    def _transition(self, task_id: str, state: str, detail: str = "") -> None:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            changed = self._set_state_locked(rec, state)
            snapshot = self._snapshot(rec) if changed else None
        if changed:
            self._emit_dict(snapshot, "progress", detail=detail or state)
            self._save(self.get(task_id))

    def _set_state_locked(self, rec: TaskRecord, state: str) -> bool:
        """Move a record's lifecycle state when the transition is legal."""
        target = str(state or "").upper()
        if target not in TASK_STATES:
            return False
        if target == rec.state:
            return False
        if target not in _ALLOWED.get(rec.state, set()):
            return False
        rec.state = target
        return True

    def _finish_stage(self, task_id: str, stage_id: str,
                      outcome: str = "") -> None:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None:
                return
            self._finish_stage_locked(rec, stage_id, outcome)
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, "step_completed", detail=outcome or stage_id,
                        step_id=stage_id)
        self._save(self.get(task_id))

    @staticmethod
    def _finish_stage_locked(rec: TaskRecord, stage_id: str, outcome: str = "",
                             default_status: str = "completed") -> None:
        step = rec.step(stage_id)
        if step is None:
            return
        if step.status in _OPEN_STEP_STATES:
            step.status = default_status
        step.finished_at = _now()
        if outcome:
            step.outcome = _clip(outcome, 240)

    def _append(self, task_id: str, field_name: str, value: Any,
                limit: int = 30) -> None:
        text = _clip(value, 400)
        if not text:
            return
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None or rec.terminal:
                return
            items: list = getattr(rec, field_name)
            if text not in items:
                items.append(text)
            del items[:-limit]
            snapshot = self._snapshot(rec)
        self._emit_dict(snapshot, "progress", detail=text[:120])
        self._save(self.get(task_id))

    def _prune_locked(self) -> None:
        while len(self._order) > _MAX_RECORDS:
            oldest = self._order.pop(0)
            self._records.pop(oldest, None)

    # ── persistence (best-effort) ─────────────────────────────────────────────
    def _connect(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self._db_path)
        return con

    def _ensure_db(self) -> None:
        if self._db_ready:
            return
        with self._lock:
            if self._db_ready:
                return
            try:
                with self._connect() as con:
                    con.execute(
                        "CREATE TABLE IF NOT EXISTS task_records ("
                        " task_id TEXT PRIMARY KEY, title TEXT, state TEXT,"
                        " category TEXT, created_at REAL, finished_at REAL,"
                        " payload TEXT, updated_at REAL)"
                    )
                self._db_ready = True
            except Exception:
                self._db_ready = False

    def _save(self, rec: TaskRecord | None) -> None:
        if rec is None:
            return
        try:
            self._ensure_db()
            if not self._db_ready:
                return
            payload = json.dumps(rec.to_dict(), ensure_ascii=False)
            with self._lock:
                con = self._connect()
                try:
                    con.execute(
                        "INSERT INTO task_records (task_id, title, state, category,"
                        " created_at, finished_at, payload, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(task_id) DO UPDATE SET title=excluded.title,"
                        " state=excluded.state, category=excluded.category,"
                        " created_at=excluded.created_at,"
                        " finished_at=excluded.finished_at,"
                        " payload=excluded.payload, updated_at=excluded.updated_at",
                        (rec.id, rec.title, rec.state, rec.category,
                         rec.created_at, rec.finished_at or 0.0, payload, _now()),
                    )
                    # Bounded store: keep only the newest _MAX_RECORDS rows.
                    con.execute(
                        "DELETE FROM task_records WHERE task_id IN ("
                        " SELECT task_id FROM task_records"
                        " ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                        (_MAX_RECORDS,),
                    )
                    con.commit()
                finally:
                    con.close()
        except Exception:
            pass

    # ── reports ───────────────────────────────────────────────────────────────
    def _write_report(self, rec: TaskRecord) -> str:
        """Write the structured markdown report; returns the path (or "")."""
        try:
            self._reports_dir.mkdir(parents=True, exist_ok=True)
            path = self._reports_dir / f"{rec.id}.md"
            path.write_text(self.render_report(rec), encoding="utf-8")
            return str(path)
        except Exception:
            return ""

    def render_report(self, rec: TaskRecord) -> str:
        """Structured §8 report: identity, timeline, checklist, answers, results.

        Contains only real execution data — no credentials (scrubbed at every
        boundary) and none of the model's private reasoning.
        """
        lines: list[str] = []
        lines.append(f"# Task report — {rec.id}")
        lines.append("")
        lines.append(f"**Title:** {_scrub(rec.title)}")
        lines.append(f"**Status:** {rec.state}")
        lines.append(f"**Source:** {rec.source}")
        if rec.category:
            lines.append(f"**Category:** {rec.category}")
        if rec.request:
            lines.append(f"**Request:** {_scrub(rec.request)}")
        if rec.plan_id:
            lines.append(f"**Linked plan:** #{_scrub(rec.plan_id)}")
        lines.append("")
        lines.append("## Timeline")
        lines.append(f"- Created: {_fmt(rec.created_at)}")
        lines.append(f"- Started: {_fmt(rec.started_at)}")
        lines.append(f"- Finished: {_fmt(rec.finished_at)}")
        lines.append(f"- Elapsed: {rec.elapsed_ms() / 1000.0:.1f}s")
        lines.append("")
        lines.append("## Checklist")
        # Analyze/validate/deliver are internal lifecycle bookkeeping stages.
        # They remain in TaskRecord to drive truthful state transitions, but
        # repeating them in every task's checklist makes distinct requests look
        # like the same static checklist. The report checklist is reserved for
        # concrete work emitted by the dispatcher (tools), clarification gates,
        # and explicitly registered operation steps.
        lifecycle_ids = {"analyze", "validate", "deliver"}
        concrete_steps = []
        for step in rec.steps:
            metadata = step.metadata if isinstance(step.metadata, dict) else {}
            is_tool = bool(metadata.get("tool"))
            is_question = step.id == "questions" or bool(metadata.get("question_ids"))
            explicitly_visible = metadata.get("visible_checklist") is True
            if (is_tool or is_question or explicitly_visible
                    or step.id.lower() not in lifecycle_ids):
                concrete_steps.append(step)
        if concrete_steps:
            for step in concrete_steps:
                mark = {"completed": "x", "failed": "!", "skipped": "-",
                        "running": "~", "waiting": "?", "pending": " "}.get(step.status, " ")
                detail = f" — {_scrub(step.outcome)}" if step.outcome else ""
                lines.append(f"- [{mark}] {_scrub(step.title)} ({step.status}){detail}")
        else:
            lines.append("- No concrete execution steps were reported by the task runner.")
        if rec.answers:
            lines.append("")
            lines.append("## Decisions that shaped the plan")
            for key, value in rec.answers.items():
                if isinstance(value, (list, tuple)):
                    value = ", ".join(str(v) for v in value)
                lines.append(f"- **{_scrub(key)}:** {_scrub(value)}")
        if rec.findings:
            lines.append("")
            lines.append("## Findings and results")
            for item in rec.findings:
                lines.append(f"- {_scrub(item)}")
        if rec.artifacts:
            lines.append("")
            lines.append("## Generated files")
            for item in rec.artifacts:
                lines.append(f"- `{_scrub(item)}`")
        if rec.warnings:
            lines.append("")
            lines.append("## Warnings")
            for item in rec.warnings:
                lines.append(f"- {_scrub(item)}")
        if rec.errors:
            lines.append("")
            lines.append("## Errors")
            for item in rec.errors:
                lines.append(f"- {_scrub(item)}")
        lines.append("")
        lines.append("## Validation")
        validate = rec.stage("validate")
        if validate is not None:
            lines.append(f"- {_scrub(validate.title)}: {validate.status}"
                         + (f" — {_scrub(validate.outcome)}" if validate.outcome else ""))
        ok_steps = sum(1 for step in concrete_steps if step.status == "completed")
        failed_steps = sum(1 for step in concrete_steps if step.status == "failed")
        skipped_steps = sum(1 for step in concrete_steps if step.status == "skipped")
        lines.append(f"- Concrete steps completed: {ok_steps}; failed: {failed_steps}; "
                     f"skipped: {skipped_steps}")
        if rec.summary:
            lines.append("")
            lines.append("## Summary")
            lines.append(_scrub(rec.summary))
        lines.append("")
        return "\n".join(lines)

    def report_text(self, task_id: str) -> str:
        """Report markdown for a record (regenerated from live state)."""
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None:
                return ""
            return self.render_report(rec)

    def report_path(self, task_id: str) -> str:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
        if rec is None or not rec.terminal:
            return ""
        return str(self._reports_dir / f"{rec.id}.md")


def _fmt(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        import datetime

        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


# ── singleton ─────────────────────────────────────────────────────────────────

_registry: TaskRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> TaskRegistry:
    """Process-wide registry (cheap; DB work is lazy and guarded)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = TaskRegistry()
    return _registry


def reset_registry() -> None:
    """Test hook: drop and recreate the singleton."""
    global _registry
    with _registry_lock:
        _registry = None
