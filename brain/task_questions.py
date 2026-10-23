"""Interactive question flow — ask only what materially changes the work.

WHAT THIS IS
    A **deterministic** question-definition system: given a real tool dispatch
    (the operation J.A.R.V.I.S. is about to perform) it decides whether an
    unresolved choice will materially affect the implementation approach, the
    files that change, or the artifact the user receives. If so it produces a
    short branching question flow; if the request is already clear it produces
    nothing and execution proceeds immediately.

§7 LIMITATION (documented on purpose)
    This build does **not** generate questions with a live language model. The
    Live session plans internally and offers no reliable pre-execution question
    hook, so the questions are authored as deterministic definitions keyed by
    operation + parameter gaps, with recommendations computed from the request
    text, the skill registry chain, the candidate files and the user's previous
    answers. Adding an LLM-assisted generator later means implementing the same
    ``QuestionDef`` contract behind a feature flag; nothing else changes.

ANSWER CHANNELS
    * the task panel (authoritative, full navigation);
    * clickable chat cards (``jarvis-answer:<task>:<question>:<option>``);
    * a spoken relay through the ``question_answer`` tool, which records the
      user's utterance as evidence and never auto-selects a recommendation.

GUARANTEES
    * a recommended answer is never treated as confirmed — the flow always
      ends on an explicit summary + confirm (or is cancelled);
    * changing an earlier answer discards only the answers that depended on it;
    * the collection gate blocks the *calling worker thread* and never the
      asyncio loop (the caller runs it in an executor);
    * a question that is not required is simply not asked — the documented
      default is used instead and recorded in the report.

Thread-safe; guarded; never raises into the dispatcher.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = [
    "OptionDef",
    "QuestionDef",
    "QuestionContext",
    "QuestionFlow",
    "QuestionSession",
    "QuestionService",
    "AnswerResult",
    "get_question_service",
    "reset_question_service",
    "analyze_request",
]

SUMMARY_ID = "summary"
CONFIRM_OPTION = "confirm"

_FILE_RE = re.compile(
    r"[\w\-./\\]+\.(?:py|js|ts|tsx|jsx|json|md|txt|csv|xlsx?|docx?|pptx?|pdf"
    r"|html|css|java|cpp|c|h|go|rs|rb|php|sql|ya?ml)\b",
    re.IGNORECASE,
)

_APPROACH_WORDS = (
    "reuse", "existing", "architecture", "refactor", "minimal", "smallest",
    "separate module", "new module", "rewrite", "in place",
)
_DEPTH_WORDS = ("quick", "brief", "short", "deep", "thorough", "comprehensive",
                "in-depth", "in depth", "all sources")
_FORMAT_WORDS = ("docx", "word", "pdf", "xlsx", "excel", "spreadsheet",
                 "pptx", "powerpoint", "slide", "presentation", "csv")
_RESEARCH_HEAVY = ("research", "compare", "analysis", "analyse", "analyze",
                   "investigate", "review", "survey", "sources", "cite")
_FIX_WORDS = ("fix", "bug", "typo", "error", "broken", "patch", "hotfix")
_NEW_WORDS = ("new", "add", "create", "build", "implement", "introduce", "tool",
              "plugin", "module", "feature")


# ── models ────────────────────────────────────────────────────────────────────

@dataclass
class QuestionContext:
    """Everything a recommendation / branching rule is allowed to look at."""

    request: str = ""
    tool: str = ""
    category: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    chain: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.request} {self.args}".lower()

    @property
    def mentioned_files(self) -> list[str]:
        return list(dict.fromkeys(m.group(0) for m in _FILE_RE.finditer(self.request or "")))

    def word_count(self) -> int:
        return len(re.findall(r"\w+", self.request or ""))


@dataclass
class OptionDef:
    """One stable answer option."""

    id: str
    label: str
    description: str = ""
    # Returns the reason string when this option is the recommended one for the
    # given context, or None when it is not recommended. Varies per context —
    # there is deliberately no single hardcoded "recommended" answer.
    recommend_when: Callable[[QuestionContext], str | None] | None = None

    def recommend_reason(self, ctx: QuestionContext) -> str | None:
        if self.recommend_when is None:
            return None
        try:
            return self.recommend_when(ctx)
        except Exception:
            return None


@dataclass
class QuestionDef:
    """One question, with its own stable id and optional branching rule."""

    id: str
    title: str
    explanation: str = ""
    options: list[OptionDef] = field(default_factory=list)
    required: bool = True
    allow_custom: bool = True
    multi_select: bool = False
    # The next question id, None for "go to the summary". Branching lives here,
    # so a decision can skip questions that its answer makes irrelevant.
    next_question: Callable[[QuestionContext], str | None] | None = None

    def option(self, option_id: str) -> OptionDef | None:
        for opt in self.options:
            if opt.id == str(option_id or ""):
                return opt
        return None

    def recommended(self, ctx: QuestionContext) -> tuple[str, str] | None:
        """(option_id, reason) for the recommended option in this context."""
        for opt in self.options:
            reason = opt.recommend_reason(ctx)
            if reason:
                return opt.id, reason
        return None

    def to_dict(self, ctx: QuestionContext | None = None) -> dict:
        rec = self.recommended(ctx) if ctx is not None else None
        return {
            "id": self.id,
            "title": self.title,
            "explanation": self.explanation,
            "required": bool(self.required),
            "allow_custom": bool(self.allow_custom),
            "multi_select": bool(self.multi_select),
            "recommended_option": rec[0] if rec else "",
            "recommended_reason": rec[1] if rec else "",
            "options": [
                {"id": o.id, "label": o.label, "description": o.description}
                for o in self.options
            ],
        }


@dataclass
class QuestionFlow:
    """A branching question set for one task (entry point + definitions)."""

    key: str
    entry_id: str
    questions: dict[str, QuestionDef]

    def entry(self) -> QuestionDef | None:
        return self.questions.get(self.entry_id)


@dataclass
class AnswerResult:
    """What ``collect`` returns once the flow finished (or was cancelled)."""

    task_id: str
    cancelled: bool = False
    stopped: bool = False
    answers: dict[str, Any] = field(default_factory=dict)     # question id -> value
    display: dict[str, str] = field(default_factory=dict)     # title -> value
    detail: dict[str, Any] = field(default_factory=dict)      # question id -> metadata
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "cancelled": bool(self.cancelled),
            "stopped": bool(self.stopped),
            "answers": dict(self.answers),
            "display": dict(self.display),
            "detail": dict(self.detail),
            "summary": self.summary,
        }


# ── deterministic flow definitions ────────────────────────────────────────────

def _approach_flow(ctx: QuestionContext) -> QuestionFlow:
    def rec_reuse(c: QuestionContext) -> str | None:
        files = c.mentioned_files
        if files:
            return (f"the request names existing code ({files[0]}), so extending it "
                    f"avoids duplicating it")
        if c.chain and not any(w in c.text for w in _NEW_WORDS + _FIX_WORDS):
            return (f"the project already routes this through the "
                    f"'{c.chain[0]}' capability, so reuse keeps one code path")
        return None

    def rec_separate(c: QuestionContext) -> str | None:
        if any(w in c.text for w in _NEW_WORDS) and not c.mentioned_files:
            return "this describes a self-contained new feature, so a separate module keeps it isolated"
        return None

    def rec_smallest(c: QuestionContext) -> str | None:
        if any(w in c.text for w in _FIX_WORDS):
            return "this is a fix, so the smallest correct change is the safest"
        return None

    approach = QuestionDef(
        id="approach",
        title="What implementation approach should I use?",
        explanation="This decides how the work is structured before anything is changed.",
        options=[
            OptionDef("reuse_existing", "Reuse the existing architecture",
                      "Extend the code that already does something similar.",
                      rec_reuse),
            OptionDef("separate_modules", "Create separate modules",
                      "Build the feature as its own self-contained module.",
                      rec_separate),
            OptionDef("smallest_change", "Make the smallest possible change",
                      "Change only what is required, nothing else.",
                      rec_smallest),
            OptionDef("other", "Other (describe it)", "Describe the approach you want."),
        ],
        required=True,
        allow_custom=True,
        next_question=_approach_next,
    )
    files = _files_question()
    return QuestionFlow(key="code_approach", entry_id="approach",
                        questions={"approach": approach, "files": files})


def _approach_next(ctx: QuestionContext) -> str | None:
    """File scope follows an approach only when that decision is still open.

    One explicitly named file is unambiguous, so the question is skipped; with
    no files named (scope open) or several named (which of them?) it is asked.
    """
    chosen = _first_option_id(ctx.answers.get("approach"))
    if chosen == "other":
        return None
    if len(ctx.mentioned_files) == 1:
        return None
    return "files"


def _files_question() -> QuestionDef:
    """The file-scope question; its options are materialised per context."""
    return QuestionDef(
        id="files",
        title="Which files may I change?",
        explanation="Only this scope will be modified.",
        options=[],                     # built by _build_file_options(ctx)
        required=True,
        allow_custom=True,
        next_question=lambda _ctx: None,
    )


def _research_flow(ctx: QuestionContext) -> QuestionFlow:
    def rec_quick(c: QuestionContext) -> str | None:
        if c.word_count() <= 8 and not any(w in c.text for w in _RESEARCH_HEAVY):
            return "this looks like a focused question, so a quick scan answers it"
        return None

    def rec_deep(c: QuestionContext) -> str | None:
        if any(w in c.text for w in _RESEARCH_HEAVY) or c.word_count() > 12:
            return "the request asks for comparison/analysis, so several sources are needed"
        return None

    depth = QuestionDef(
        id="depth",
        title="How deep should the research go?",
        explanation="This decides how many sources are consulted before I answer.",
        options=[
            OptionDef("quick", "Quick scan", "A handful of sources, the short answer.", rec_quick),
            OptionDef("deep", "Deep dive", "Multiple sources, compared and cited.", rec_deep),
        ],
        required=True,
        allow_custom=False,
        next_question=lambda _ctx: None,
    )
    return QuestionFlow(key="research_depth", entry_id="depth",
                        questions={"depth": depth})


def _report_flow(ctx: QuestionContext) -> QuestionFlow:
    def slides(c: QuestionContext) -> str | None:
        if any(w in c.text for w in ("slide", "presentation", "deck")):
            return "a presentation was requested"
        return None

    def sheet(c: QuestionContext) -> str | None:
        if any(w in c.text for w in ("spreadsheet", "excel", "data", "table", "csv")):
            return "the content is tabular"
        return None

    def doc(c: QuestionContext) -> str | None:
        if any(w in c.text for w in ("report", "letter", "document", "memo", "notes")):
            return "a written document was requested"
        return None

    def pdf(c: QuestionContext) -> str | None:
        if any(w in c.text for w in ("print", "share", "send", "pdf")):
            return "the document is meant to be shared/printed"
        return None

    question = QuestionDef(
        id="format",
        title="Which format should the document be in?",
        explanation="This decides the file I generate.",
        options=[
            OptionDef("docx", "Word document (.docx)", "Editable text document.", doc),
            OptionDef("pptx", "PowerPoint (.pptx)", "Slide deck.", slides),
            OptionDef("xlsx", "Excel workbook (.xlsx)", "Spreadsheet of the data.", sheet),
            OptionDef("pdf", "PDF (.pdf)", "Ready to print or share.", pdf),
        ],
        required=True,
        allow_custom=False,
        next_question=lambda _ctx: None,
    )
    return QuestionFlow(key="report_format", entry_id="format",
                        questions={"format": question})


# ── deterministic analysis: does this dispatch need questions at all? ─────────

def _first_option_id(value: Any) -> str:
    if isinstance(value, dict):
        opts = value.get("option_ids") or []
        return str(opts[0]) if opts else ""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")


def _gate_enabled() -> bool:
    try:
        from config.settings import feature_enabled  # noqa: PLC0415

        return bool(feature_enabled("task_questions"))
    except Exception:
        return True


def _plan_chain(request: str) -> list[str]:
    try:
        from core.brain_bridge import plan_request  # noqa: PLC0415

        plan = plan_request(request) or {}
        return [str(s) for s in (plan.get("steps") or [])][:4]
    except Exception:
        return []


def _features() -> dict:
    try:
        from core.brain_bridge import current_features  # noqa: PLC0415

        return current_features()
    except Exception:
        return {}


def _code_action(args: dict) -> bool:
    action = str((args or {}).get("action") or "").lower()
    return action in ("", "write", "edit", "build", "create", "implement", "run",
                      "execute", "refactor", "optimize", "generate")


def build_flow(tool: str, args: dict | None, request: str,
               category: str = "") -> QuestionFlow | None:
    """Deterministic entry point: the flow for this dispatch, or None.

    Returns None (execute immediately) whenever the request already resolves
    the material choices — which is the common case.
    """
    if not _gate_enabled():
        return None
    args = dict(args or {})
    request = str(request or "")
    low = request.lower()
    ctx = QuestionContext(request=request, tool=str(tool or ""),
                          category=str(category or ""), args=args,
                          chain=_plan_chain(request), features=_features())
    tool = str(tool or "").lower()

    flow: QuestionFlow | None = None
    # Coding: the approach is material only when the request does not state one.
    if tool in ("dev_agent", "code_helper", "coding_agent") and _code_action(args):
        if not any(w in low for w in _APPROACH_WORDS):
            flow = _approach_flow(ctx)

    # Research depth is material only for substantial research.
    elif tool in ("web_research", "research") and (
            any(w in low for w in _RESEARCH_HEAVY) or ctx.word_count() > 12):
        if not any(w in low for w in _DEPTH_WORDS):
            flow = _research_flow(ctx)

    # Document format is material unless the request names one.
    elif tool == "documents" and not any(w in low for w in _FORMAT_WORDS):
        if not str(args.get("format") or args.get("type") or "").strip():
            flow = _report_flow(ctx)

    if flow is None:
        return None
    return _context_variants(flow, ctx)


def _context_variants(flow: QuestionFlow, ctx: QuestionContext) -> QuestionFlow:
    """Materialise context-dependent questions (the FILES option list)."""
    files_q = flow.questions.get("files")
    if files_q is not None and not files_q.options:
        files_q.options = _build_file_options(ctx)
    return flow


def _build_file_options(ctx: QuestionContext) -> list[OptionDef]:
    def rec_named(c: QuestionContext) -> str | None:
        n = len(c.mentioned_files)
        if 1 <= n <= 5:
            return ("you named a defined set of files, so limiting the change to "
                    "exactly those is the safest scope")
        return None

    def rec_module(c: QuestionContext) -> str | None:
        if any(w in c.text for w in ("refactor", "architecture", "reuse")):
            return "the chosen approach touches several call sites in the same module"
        return None

    def rec_per_file(c: QuestionContext) -> str | None:
        if len(c.mentioned_files) > 5:
            return "many files were named, so confirming each keeps the change reviewable"
        return None

    named = ctx.mentioned_files
    opts: list[OptionDef] = []
    if named:
        opts.append(OptionDef("named_only", f"Only the files I named ({len(named)})",
                              ", ".join(named[:4]) + ("…" if len(named) > 4 else ""),
                              rec_named))
        opts.append(OptionDef("affected_module", "The affected module",
                              "Any file in the module these belong to.", rec_module))
    else:
        opts.append(OptionDef("named_only", "Only files I explicitly name",
                              "I will confirm each path before touching it.", rec_named))
        opts.append(OptionDef("affected_module", "The affected module",
                              "Whatever files that work requires.", rec_module))
    opts.append(OptionDef("ask_per_file", "Ask me before each file",
                          "I will confirm every file individually.", rec_per_file))
    opts.append(OptionDef("other", "Other (describe it)", "Describe the scope you want."))
    return opts


# ── the session (per task) ────────────────────────────────────────────────────

class QuestionSession:
    """One task's question walk: ask → branch → summary → confirm/cancel."""

    def __init__(self, task_id: str, flow: QuestionFlow, ctx: QuestionContext):
        self.task_id = str(task_id or "")
        self.flow = flow
        self.ctx = ctx
        self.state = "ASKING"                 # ASKING | SUMMARY | CONFIRMED | CANCELLED
        self.answers: dict[str, Any] = {}
        self._history: list[str] = []
        self._current = flow.entry_id
        self.release = threading.Event()
        self._lock = threading.RLock()

    # ── reads ─────────────────────────────────────────────────────────────────
    @property
    def current_id(self) -> str:
        return self._current

    def current(self) -> QuestionDef | None:
        with self._lock:
            if self.state != "ASKING":
                return None
            return self.flow.questions.get(self._current)

    def answered_ids(self) -> list[str]:
        return [q for q in self.flow.questions if q in self.answers]

    def validation_error(self, question: QuestionDef) -> str:
        """Why the current answer is not acceptable yet ("" when it is)."""
        value = self.answers.get(question.id)
        if value is None:
            return "This question needs an answer to continue." if question.required else ""
        option_ids = _value_option_ids(value)
        custom = _value_custom(value)
        if not option_ids and not custom:
            return "Choose an option or type an answer." if question.required else ""
        if question.required and question.multi_select and not option_ids and not custom:
            return "Select at least one option."
        if "other" in option_ids and question.allow_custom and not custom.strip():
            return "Describe the 'other' option you chose."
        return ""

    def display_value(self, question_id: str) -> str:
        question = self.flow.questions.get(question_id)
        value = self.answers.get(question_id)
        if question is None or value is None:
            return ""
        option_ids = _value_option_ids(value)
        labels = []
        for oid in option_ids:
            opt = question.option(oid)
            labels.append(opt.label if opt else str(oid))
        custom = _value_custom(value)
        if custom:
            labels.append(f'"{custom}"')
        return ", ".join(labels) if labels else ""

    def summary_text(self) -> str:
        lines = []
        for qid, question in self.flow.questions.items():
            if qid not in self.answers:
                continue
            lines.append(f"- {question.title} → {self.display_value(qid)}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        with self._lock:
            question = self.current()
            ctx = self._context()
            return {
                "task_id": self.task_id,
                "flow": self.flow.key,
                "state": self.state,
                "current_id": self._current if self.state == "ASKING" else "",
                "question": question.to_dict(ctx) if question is not None else None,
                "can_go_back": bool(self._history),
                "answered": {qid: self.display_value(qid) for qid in self.answers},
                "answered_detail": {
                    qid: {"option_ids": _value_option_ids(self.answers.get(qid)),
                          "custom": _value_custom(self.answers.get(qid))}
                    for qid in self.answers
                },
                "summary": self.summary_text(),
                "validation_error": (
                    self.validation_error(question) if question is not None else ""),
            }

    # ── mutations ─────────────────────────────────────────────────────────────
    def _context(self) -> QuestionContext:
        return QuestionContext(
            request=self.ctx.request, tool=self.ctx.tool, category=self.ctx.category,
            args=dict(self.ctx.args), answers=dict(self.answers),
            chain=list(self.ctx.chain), features=dict(self.ctx.features),
        )

    def _reachable_from(self, question_id: str, answers: dict) -> set[str]:
        """Questions the given answers would still reach (dependent set)."""
        seen: set[str] = set()
        ctx = QuestionContext(
            request=self.ctx.request, tool=self.ctx.tool, category=self.ctx.category,
            args=dict(self.ctx.args), answers=dict(answers),
            chain=list(self.ctx.chain), features=dict(self.ctx.features),
        )
        nxt = self.flow.questions.get(question_id)
        guard = 0
        nxt_id = nxt.next_question(ctx) if nxt and nxt.next_question else None
        while nxt_id and nxt_id in self.flow.questions and guard < 20:
            if nxt_id in seen:
                break
            seen.add(nxt_id)
            nxt = self.flow.questions.get(nxt_id)
            nxt_id = nxt.next_question(ctx) if nxt and nxt.next_question else None
            guard += 1
        return seen

    def answer(self, question_id: str, option_ids: Iterable[str] | None = None,
               custom: str = "", source: str = "panel",
               evidence: str = "") -> str:
        """Store an answer and advance. Returns "" or a validation error."""
        with self._lock:
            if self.state != "ASKING":
                return "This flow is already closed."
            question = self.flow.questions.get(str(question_id or ""))
            if question is None:
                return f"Unknown question '{question_id}'."
            opts = [str(o) for o in (option_ids or []) if str(o)]
            for oid in opts:
                if question.option(oid) is None:
                    return f"Unknown option '{oid}'."
            if not question.multi_select and len(opts) > 1:
                opts = opts[:1]
            value = {"option_ids": opts, "custom": str(custom or "").strip(),
                     "source": str(source or "panel"),
                     "evidence": str(evidence or "")[:300]}
            previous = self.answers.get(question.id)
            self.answers[question.id] = value
            error = self.validation_error(question)
            if error:
                if previous is None:
                    self.answers.pop(question.id, None)
                else:
                    self.answers[question.id] = previous
                return error
            self._invalidate_dependents(question.id, previous, value)
            nxt = question.next_question(self._context()) if question.next_question else None
            self._history.append(question.id)
            if nxt and nxt in self.flow.questions:
                self._current = nxt
            else:
                self.state = "SUMMARY"
            return ""

    def _invalidate_dependents(self, question_id: str, previous: Any,
                               value: Any) -> None:
        """Drop only the answers invalidated by a changed earlier answer."""
        if previous is None:
            return
        old = dict(self.answers)
        old[question_id] = previous
        old_reach = self._reachable_from(question_id, old)
        new_reach = self._reachable_from(question_id, self.answers)
        for qid in old_reach - new_reach:
            if qid != question_id:
                self.answers.pop(qid, None)

    def back(self) -> str:
        """Return to the previous question (answers are kept until replaced)."""
        with self._lock:
            if self.state == "SUMMARY":
                if not self._history:
                    return "There is no previous question."
                self.state = "ASKING"
                self._current = self._history[-1]
                return ""
            if self.state != "ASKING" or not self._history:
                return "There is no previous question."
            self._history.pop()
            self._current = self._history[-1] if self._history else self.flow.entry_id
            return ""

    def skip(self) -> str:
        """Skip the current OPTIONAL question (required ones cannot be skipped)."""
        with self._lock:
            question = self.current()
            if question is None:
                return "There is no open question."
            if question.required:
                return "This question is required and cannot be skipped."
            nxt = question.next_question(self._context()) if question.next_question else None
            self.answers[question.id] = {"option_ids": [], "custom": "",
                                         "source": "skip", "evidence": ""}
            self._history.append(question.id)
            if nxt and nxt in self.flow.questions:
                self._current = nxt
            else:
                self.state = "SUMMARY"
            return ""

    def edit(self, question_id: str) -> str:
        """Jump back to an already-answered question to change it."""
        with self._lock:
            if self.state in ("CONFIRMED", "CANCELLED"):
                return "This flow is already closed."
            if question_id not in self.flow.questions:
                return f"Unknown question '{question_id}'."
            self.state = "ASKING"
            self._current = str(question_id)
            self._history = [q for q in self._history if q != question_id]
            return ""

    def _is_answered(self, question_id: str) -> bool:
        value = self.answers.get(question_id)
        if value is None:
            return False
        return bool(_value_option_ids(value) or _value_custom(value))

    def _missing_required(self) -> list[str]:
        """Required questions on the CURRENT path that still need an answer.

        Branching legitimately skips questions (choosing "other" never reaches
        the file-scope question), so confirmation must walk the live path — not
        demand an answer to every question in the flow.
        """
        ctx = self._context()
        question_id = self.flow.entry_id
        guard = 0
        while question_id and question_id in self.flow.questions and guard < 30:
            guard += 1
            question = self.flow.questions[question_id]
            if question.required and not self._is_answered(question.id):
                return [question_id]
            nxt = (question.next_question(ctx) if question.next_question else None)
            if not nxt or nxt not in self.flow.questions:
                break
            question_id = nxt
        return []

    def confirm(self) -> str:
        with self._lock:
            if self.state != "SUMMARY":
                return "Answer every required question before confirming."
            missing = self._missing_required()
            if missing:
                self.state = "ASKING"
                self._current = missing[0]
                return "Answer every required question before confirming."
            self.state = "CONFIRMED"
            self.release.set()
            return ""

    def cancel(self, reason: str = "") -> None:
        with self._lock:
            if self.state in ("CONFIRMED", "CANCELLED"):
                return
            self.state = "CANCELLED"
            self.cancel_reason = str(reason or "")
            self.release.set()

    @property
    def closed(self) -> bool:
        return self.state in ("CONFIRMED", "CANCELLED")


def _value_option_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(o) for o in (value.get("option_ids") or [])]
    if isinstance(value, (list, tuple)):
        return [str(o) for o in value]
    return [str(value)] if value else []


def _value_custom(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("custom") or "")
    return ""


# ── the service ───────────────────────────────────────────────────────────────

class QuestionService:
    """Per-task question sessions + the blocking collection gate (UI-bound)."""

    def __init__(self) -> None:
        self._sessions: dict[str, QuestionSession] = {}
        self._lock = threading.RLock()
        self._show_panel: Callable[[dict], None] | None = None
        self._show_card: Callable[[dict], None] | None = None
        self._log: Callable[[str], None] | None = None

    # ── wiring (the security/confirm.py pattern: main.py binds these) ─────────
    def bind(self, show_panel=None, show_card=None, log=None) -> None:
        self._show_panel = show_panel if callable(show_panel) else None
        self._show_card = show_card if callable(show_card) else None
        self._log = log if callable(log) else None

    def _emit_log(self, msg: str) -> None:
        if self._log:
            try:
                self._log(str(msg))
            except Exception:
                pass

    def _notify(self, session: QuestionSession) -> None:
        """Push the session snapshot to the panel and the current question card."""
        try:
            snap = session.to_dict()
        except Exception:
            return
        if self._show_panel:
            try:
                self._show_panel(snap)
            except Exception:
                pass
        if self._show_card and session.state == "ASKING":
            try:
                self._show_card(snap)
            except Exception:
                pass

    # ── analysis ──────────────────────────────────────────────────────────────
    def analyze(self, request: str, tool: str = "", args: dict | None = None,
                category: str = "") -> QuestionFlow | None:
        """The deterministic flow for this dispatch, or None for 'no questions'."""
        try:
            return build_flow(tool, args, request, category)
        except Exception:
            return None

    # ── the gate ──────────────────────────────────────────────────────────────
    def collect(self, task_id: str, flow: QuestionFlow, *,
                request: str = "", tool: str = "") -> AnswerResult:
        """Block the CALLING WORKER THREAD until the flow is confirmed/cancelled.

        The caller is responsible for running this off the asyncio loop (e.g.
        ``loop.run_in_executor(None, ...)``), exactly like the coding-approval
        gate. There is no timeout: a required question waits for the user, who
        can always cancel from the panel, by voice, or by interrupting.
        """
        ctx = QuestionContext(request=str(request or ""), tool=str(tool or ""),
                              chain=_plan_chain(str(request or "")),
                              features=_features())
        session = QuestionSession(str(task_id or ""), flow, ctx)
        with self._lock:
            # One session per task: a stale one from a previous turn is dropped.
            self._sessions[str(task_id or "")] = session
        self._emit_log(f"SYS: Waiting for your answers — {len(flow.questions)} question(s)")
        self._notify(session)
        session.release.wait()          # blocking is intentional (worker thread)
        with self._lock:
            self._sessions.pop(str(task_id or ""), None)
        return self._result(session)

    def _result(self, session: QuestionSession) -> AnswerResult:
        if session.state == "CANCELLED":
            return AnswerResult(task_id=session.task_id, cancelled=True,
                                stopped=True,
                                summary=getattr(session, "cancel_reason", "")
                                or "clarification cancelled")
        answers = {qid: session.display_value(qid) for qid in session.answers}
        detail = {qid: dict(session.answers[qid]) for qid in session.answers}
        return AnswerResult(task_id=session.task_id, cancelled=False,
                            answers=answers, display=dict(answers), detail=detail,
                            summary=session.summary_text())

    # ── non-blocking channels ─────────────────────────────────────────────────
    def get_session(self, task_id: str) -> QuestionSession | None:
        with self._lock:
            return self._sessions.get(str(task_id or ""))

    def session_snapshot(self, task_id: str) -> dict:
        session = self.get_session(task_id)
        return session.to_dict() if session is not None else {}

    def resolve_from_panel(self, task_id: str, question_id: str,
                           option_ids: Iterable[str] | None = None,
                           custom: str = "") -> str:
        return self._apply(task_id, question_id, option_ids, custom, source="panel")

    def resolve_from_chat(self, task_id: str, question_id: str,
                          option_id: str) -> str:
        """A chat-card click. Multi-select toggles; single-select advances."""
        session = self.get_session(task_id)
        if session is None:
            return "That question is no longer open."
        question = session.flow.questions.get(str(question_id or ""))
        if question is None:
            return "Unknown question."
        current = _value_option_ids(session.answers.get(question.id))
        if question.multi_select:
            if option_id in current:
                current = [o for o in current if o != option_id]
            else:
                current.append(option_id)
            return self._apply(task_id, question_id, current, "", source="card",
                               advance=False)
        return self._apply(task_id, question_id, [option_id], "", source="card")

    def relay_voice_answer(self, task_id: str, question_id: str,
                           option_ids: Iterable[str] | None = None,
                           custom: str = "", evidence: str = "") -> str:
        """The model relays a spoken answer. Never auto-selects a recommendation."""
        session = self.get_session(task_id)
        if session is None:
            return "There is no open question for this task."
        qid = str(question_id or "")
        if qid in ("", SUMMARY_ID, "confirm", "done"):
            return self.confirm(task_id)
        return self._apply(task_id, qid, option_ids, custom, source="voice",
                           evidence=evidence)

    def back(self, task_id: str) -> str:
        session = self.get_session(task_id)
        if session is None:
            return "There is no open question for this task."
        error = session.back()
        self._notify(session)
        return error

    def skip(self, task_id: str) -> str:
        """Skip the current OPTIONAL question (required ones are refused)."""
        session = self.get_session(task_id)
        if session is None:
            return "There is no open question for this task."
        error = session.skip()
        self._notify(session)
        return error

    def confirm(self, task_id: str) -> str:
        session = self.get_session(task_id)
        if session is None:
            return "There is no open question for this task."
        if session.state == "ASKING":
            return ("Answer every question first, then confirm the summary.")
        error = session.confirm()
        self._notify(session)
        return error or "Confirmed. I will proceed with those decisions."

    def cancel(self, task_id: str, reason: str = "user cancelled") -> str:
        session = self.get_session(task_id)
        if session is None:
            return "There is no open question for this task."
        session.cancel(reason)
        self._notify(session)
        return "Cancelled. I will not start that work."

    def cancel_all(self, reason: str = "session ended") -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.cancel(reason)

    def _apply(self, task_id: str, question_id: str,
               option_ids: Iterable[str] | None, custom: str, *,
               source: str = "panel", evidence: str = "",
               advance: bool = True) -> str:
        session = self.get_session(task_id)
        if session is None:
            return "There is no open question for this task."
        if not advance and session.state == "ASKING":
            # Multi-select toggle: store without advancing, re-render the card.
            question = session.flow.questions.get(str(question_id))
            if question is not None:
                session.answers[question.id] = {
                    "option_ids": [str(o) for o in (option_ids or [])],
                    "custom": str(custom or "").strip(),
                    "source": source, "evidence": str(evidence or "")[:300],
                }
                self._notify(session)
                return ""
        error = session.answer(question_id, option_ids, custom, source=source,
                               evidence=evidence)
        self._notify(session)
        return error

    def reset(self) -> None:
        """Test hook."""
        with self._lock:
            self._sessions.clear()


_service: QuestionService | None = None
_service_lock = threading.Lock()


def get_question_service() -> QuestionService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = QuestionService()
    return _service


def reset_question_service() -> None:
    global _service
    with _service_lock:
        _service = None


def analyze_request(request: str, tool: str = "", args: dict | None = None,
                    category: str = "") -> QuestionFlow | None:
    """Module-level convenience for the dispatcher."""
    return get_question_service().analyze(request, tool, args, category)
