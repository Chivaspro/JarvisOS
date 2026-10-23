"""Deterministic question definitions — when to ask, and what to recommend.

These tests pin the two properties the feature is judged on: questions appear
only when the request leaves a material choice open, and the recommendation is
computed from the real request context (never one fixed answer for every
question).
"""
from __future__ import annotations

import pytest

from brain import task_questions as tq


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    # The gate must be on for the definitions to be produced.
    monkeypatch.setattr(tq, "_gate_enabled", lambda: True)


def _flow(tool, request, args=None, category=""):
    return tq.build_flow(tool, args or {}, request, category)


# ── when NOT to ask ───────────────────────────────────────────────────────────

def test_clear_coding_request_is_not_questioned():
    flow = _flow("dev_agent", "refactor the parser using the existing architecture",
                 {"description": "refactor the parser"})
    assert flow is None


def test_clear_research_request_is_not_questioned():
    flow = _flow("web_research", "do a deep dive on quantum error correction")
    assert flow is None


def test_document_with_named_format_is_not_questioned():
    flow = _flow("documents", "create the quarterly report as a pdf")
    assert flow is None
    flow2 = _flow("documents", "make the summary", {"format": "xlsx"})
    assert flow2 is None


def test_simple_lookup_is_not_questioned():
    flow = _flow("web_research", "who won the 1994 world cup")
    assert flow is None


def test_unrelated_tool_never_triggers_questions():
    assert _flow("play_music", "play something") is None
    assert _flow("system_status", "how is the cpu") is None


def test_gate_flag_off_disables_every_question(monkeypatch):
    monkeypatch.setattr(tq, "_gate_enabled", lambda: False)
    assert _flow("dev_agent", "build a new analytics module") is None
    assert _flow("documents", "write a summary") is None


# ── when TO ask ───────────────────────────────────────────────────────────────

def test_ambiguous_coding_request_asks_for_approach():
    flow = _flow("dev_agent", "add a plugin that streams camera frames",
                 {"description": "add a plugin"})
    assert flow is not None
    assert flow.key == "code_approach"
    assert flow.entry_id == "approach"
    question = flow.entry()
    assert question is not None
    assert question.required
    assert [o.id for o in question.options] == [
        "reuse_existing", "separate_modules", "smallest_change", "other",
    ]


def test_substantial_research_asks_for_depth():
    flow = _flow("web_research",
                 "research and compare the leading vector databases for our stack")
    assert flow is not None
    assert flow.key == "research_depth"
    assert flow.entry_id == "depth"


def test_documents_without_format_asks_for_format():
    flow = _flow("documents", "write up the findings from the audit")
    assert flow is not None
    assert flow.key == "report_format"


# ── recommendations vary with context ────────────────────────────────────────

def test_recommendation_varies_across_requests():
    new_feature = _flow("dev_agent", "add a brand new plugin for camera streaming")
    fix_it = _flow("dev_agent", "fix the crash in the worker")
    touch_existing = _flow("dev_agent", "extend the parser in parser.py")

    rec_new = new_feature.entry().recommended(
        tq.QuestionContext(request="add a brand new plugin for camera streaming"))
    rec_fix = fix_it.entry().recommended(
        tq.QuestionContext(request="fix the crash in the worker"))
    rec_existing = touch_existing.entry().recommended(
        tq.QuestionContext(request="extend the parser in parser.py"))

    picks = {rec_new[0], rec_fix[0], rec_existing[0]}
    assert rec_new[0] == "separate_modules"
    assert rec_fix[0] == "smallest_change"
    assert rec_existing[0] == "reuse_existing"
    assert len(picks) == 3, "the same answer must not be recommended everywhere"
    # Every recommendation carries its reason.
    for rec in (rec_new, rec_fix, rec_existing):
        assert rec[1]


def test_report_format_recommendation_follows_the_content():
    ctx_slides = tq.QuestionContext(request="turn this into a presentation deck")
    ctx_data = tq.QuestionContext(request="summarise the sales data in a table")
    ctx_doc = tq.QuestionContext(request="write the project report")
    flow = _flow("documents", "produce the output")
    question = flow.entry()
    assert question.recommended(ctx_slides)[0] == "pptx"
    assert question.recommended(ctx_data)[0] == "xlsx"
    assert question.recommended(ctx_doc)[0] == "docx"


def test_research_depth_recommendation_follows_the_request():
    flow = _flow("web_research", "research and compare the leading databases")
    question = flow.entry()
    assert question.recommended(
        tq.QuestionContext(request="research and compare the leading databases"))[0] == "deep"
    assert question.recommended(
        tq.QuestionContext(request="what is the capital of peru"))[0] == "quick"


# ── question shape ────────────────────────────────────────────────────────────

def test_files_question_options_follow_the_named_files():
    flow = _flow("dev_agent", "add a plugin touching parser.py and tokenizer.py "
                              "and lexer.py and emitter.py and ast_nodes.py and cli.py")
    # Materialised options list the named files.
    files = flow.questions.get("files")
    assert files is not None
    assert files.options
    assert files.options[0].id == "named_only"
    assert "parser.py" in files.options[0].description


def test_question_serialises_with_stable_option_ids():
    flow = _flow("documents", "write the project report")
    data = flow.entry().to_dict(tq.QuestionContext(request="write the project report"))
    assert data["id"] == "format"
    assert [o["id"] for o in data["options"]] == ["docx", "pptx", "xlsx", "pdf"]
    assert data["recommended_option"] == "docx"
    assert data["recommended_reason"]


def test_analysis_never_raises_on_bad_input():
    assert tq.build_flow(None, None, None) is None
    assert tq.build_flow("dev_agent", {"action": "write"}, "") is not None


def test_optional_questions_are_supported_by_the_model():
    question = tq.QuestionDef(id="extra", title="Optional?", required=False)
    assert question.to_dict()["required"] is False
