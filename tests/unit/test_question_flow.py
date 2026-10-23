"""Interactive question flow — branching, invalidation, navigation, the gate.

Scenario coverage from the delivery spec: recommended answer, alternative
answer, custom answer, changing a previous answer (invalidating only dependents),
dynamic follow-ups, validation, summary/confirm, chat and voice channels, and
cancel-without-timeout.
"""
from __future__ import annotations

import threading
import time

import pytest

from brain import task_questions as tq
from brain.task_questions import (
    QuestionContext, QuestionDef, QuestionFlow, QuestionService, OptionDef,
    SUMMARY_ID,
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(tq, "_gate_enabled", lambda: True)


@pytest.fixture()
def service():
    svc = QuestionService()
    yield svc
    svc.reset()


def _approach_flow(request="add a plugin for camera streaming"):
    return tq.build_flow("dev_agent", {"description": request}, request)


# ── branching & dynamic follow-ups ───────────────────────────────────────────

def test_answers_branch_to_the_follow_up_question():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext(request="add a plugin"))
    assert session.current_id == "approach"
    assert session.answer("approach", ["reuse_existing"]) == ""
    assert session.current_id == "files", "file scope follows an approach"

    assert session.answer("files", ["named_only"]) == ""
    assert session.state == "SUMMARY"


def test_one_named_file_skips_the_follow_up_question():
    flow = _approach_flow("extend the parser in parser.py")
    session = tq.QuestionSession("T1", flow, QuestionContext(request="extend the parser in parser.py"))
    assert session.answer("approach", ["reuse_existing"]) == ""
    assert session.state == "SUMMARY", "an unambiguous scope skips the file question"


def test_other_approach_does_not_ask_about_files():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext(request="add a plugin"))
    assert session.answer("approach", ["other"], custom="do it as a script") == ""
    assert session.state == "SUMMARY"


# ── changing an earlier answer (dependent invalidation) ───────────────────────

def test_changing_an_earlier_answer_drops_only_its_dependents():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext(request="add a plugin"))
    session.answer("approach", ["reuse_existing"])
    session.answer("files", ["named_only"])
    assert set(session.answers) == {"approach", "files"}

    # Re-open the first question and pick an answer that needs no file scope.
    assert session.edit("approach") == ""
    assert session.answer("approach", ["other"], custom="just a sketch") == ""
    assert "files" not in session.answers, "the dependent answer was discarded"
    assert "approach" in session.answers, "the changed answer is kept"
    assert session.state == "SUMMARY"


def test_unrelated_answers_are_preserved_across_a_change():
    # A hand-built flow with two independent branches.
    q1 = QuestionDef(id="q1", title="First?",
                     options=[OptionDef("a", "A"), OptionDef("b", "B")],
                     next_question=lambda ctx: None)
    q2 = QuestionDef(id="q2", title="Second?", required=False,
                     options=[OptionDef("x", "X"), OptionDef("y", "Y")],
                     next_question=lambda ctx: None)
    flow = QuestionFlow(key="custom", entry_id="q1", questions={"q1": q1, "q2": q2})
    session = tq.QuestionSession("T1", flow, QuestionContext())
    session.answer("q1", ["a"])
    session.answers["q2"] = {"option_ids": ["x"], "custom": "", "source": "panel",
                             "evidence": ""}
    session.edit("q1")
    session.answer("q1", ["b"])
    assert session.answers["q2"]["option_ids"] == ["x"], "unrelated answer kept"


# ── validation ────────────────────────────────────────────────────────────────

def test_required_question_rejects_an_empty_answer():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext())
    error = session.answer("approach", [])
    assert error
    assert "approach" not in session.answers


def test_custom_other_requires_descriptive_text():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext())
    assert session.answer("approach", ["other"], custom="") != ""
    assert session.answer("approach", ["other"], custom="a small script") == ""


def test_unknown_question_or_option_is_refused():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext())
    assert session.answer("nope", ["reuse_existing"]) != ""
    assert session.answer("approach", ["not_an_option"]) != ""


def test_multi_select_requires_at_least_one_when_required():
    question = QuestionDef(id="pick", title="Pick", required=True, multi_select=True,
                           options=[OptionDef("a", "A"), OptionDef("b", "B")])
    flow = QuestionFlow(key="custom", entry_id="pick", questions={"pick": question})
    session = tq.QuestionSession("T1", flow, QuestionContext())
    assert session.answer("pick", []) != ""
    assert session.answer("pick", ["a", "b"]) == ""
    assert session.state == "SUMMARY"


# ── navigation & summary ──────────────────────────────────────────────────────

def test_back_returns_to_the_previous_question():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext())
    session.answer("approach", ["reuse_existing"])
    assert session.current_id == "files"
    assert session.back() == ""
    assert session.current_id == "approach"


def test_summary_lists_every_decision():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext(request="add a plugin"))
    session.answer("approach", ["reuse_existing"])
    session.answer("files", ["named_only"])
    text = session.summary_text()
    assert "Reuse the existing architecture" in text
    assert "Only files I explicitly name" in text


def test_confirm_requires_the_summary_and_completes_the_flow():
    flow = _approach_flow()
    session = tq.QuestionSession("T1", flow, QuestionContext())
    assert session.confirm() != ""          # still asking
    session.answer("approach", ["reuse_existing"])
    session.answer("files", ["named_only"])
    assert session.confirm() == ""
    assert session.state == "CONFIRMED"
    assert session.release.is_set()


def test_required_question_missing_blocks_confirmation():
    question = QuestionDef(id="q", title="Q", required=True,
                           options=[OptionDef("a", "A")])
    flow = QuestionFlow(key="custom", entry_id="q", questions={"q": question})
    session = tq.QuestionSession("T1", flow, QuestionContext())
    session.state = "SUMMARY"                # force the state to prove the guard
    assert session.confirm() != ""
    assert session.state == "ASKING"


def test_optional_question_can_be_skipped_but_required_cannot():
    optional = QuestionDef(id="o", title="Optional", required=False,
                           options=[OptionDef("a", "A")])
    flow = QuestionFlow(key="custom", entry_id="o", questions={"o": optional})
    session = tq.QuestionSession("T1", flow, QuestionContext())
    assert session.skip() == ""
    assert session.state == "SUMMARY"

    required = QuestionDef(id="r", title="Required", required=True,
                           options=[OptionDef("a", "A")])
    flow2 = QuestionFlow(key="custom2", entry_id="r", questions={"r": required})
    session2 = tq.QuestionSession("T2", flow2, QuestionContext())
    assert session2.skip() != ""


# ── the blocking gate ─────────────────────────────────────────────────────────

def test_collect_returns_confirmed_answers(service):
    flow = _approach_flow()
    result_holder = {}

    def run():
        result_holder["result"] = service.collect("T-1", flow, request="add a plugin")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    _wait_for_session(service, "T-1")

    assert service.resolve_from_panel("T-1", "approach", ["reuse_existing"]) == ""
    assert service.resolve_from_panel("T-1", "files", ["named_only"]) == ""
    assert service.confirm("T-1").startswith("Confirmed")
    worker.join(timeout=5)

    result = result_holder["result"]
    assert not result.cancelled
    assert result.answers["approach"] == "Reuse the existing architecture"
    assert result.answers["files"] == "Only files I explicitly name"
    assert "Reuse the existing architecture" in result.summary


def test_collect_returns_cancelled_without_confirming(service):
    flow = _approach_flow()
    holder = {}

    def run():
        holder["result"] = service.collect("T-2", flow, request="add a plugin")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    _wait_for_session(service, "T-2")
    service.cancel("T-2", "user interrupted")
    worker.join(timeout=5)
    assert holder["result"].cancelled


def test_collect_has_no_timeout_and_waits_for_the_user(service):
    flow = _approach_flow()
    done = threading.Event()

    def run():
        service.collect("T-3", flow, request="add a plugin")
        done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    _wait_for_session(service, "T-3")
    time.sleep(0.4)
    assert not done.is_set(), "the gate never auto-confirms a recommendation"
    service.cancel("T-3")
    worker.join(timeout=5)
    assert done.is_set()


def test_chat_card_click_answers_and_advances(service):
    flow = _approach_flow()
    holder = {}

    def run():
        holder["result"] = service.collect("T-4", flow, request="add a plugin")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    _wait_for_session(service, "T-4")
    assert service.resolve_from_chat("T-4", "approach", "reuse_existing") == ""
    assert service.session_snapshot("T-4")["current_id"] == "files"
    service.resolve_from_chat("T-4", "files", "named_only")
    service.confirm("T-4")
    worker.join(timeout=5)
    assert holder["result"].answers["approach"] == "Reuse the existing architecture"


def test_voice_relay_records_evidence_and_never_auto_confirms(service):
    flow = _approach_flow()
    holder = {}

    def run():
        holder["result"] = service.collect("T-5", flow, request="add a plugin")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    _wait_for_session(service, "T-5")

    msg = service.relay_voice_answer("T-5", "approach", ["separate_modules"],
                                     evidence="make it a separate module please")
    assert msg == ""
    detail = service.session_snapshot("T-5")
    assert detail["current_id"] == "files"
    # The recommendation was NOT auto-applied; the spoken selection is stored.
    session = service.get_session("T-5")
    assert session.answers["approach"]["source"] == "voice"
    assert "separate module" in session.answers["approach"]["evidence"]

    service.relay_voice_answer("T-5", "files", ["named_only"], evidence="only the plugin file")
    # Still not confirmed — the flow requires the explicit confirm step.
    assert service.get_session("T-5").state == "SUMMARY"
    service.relay_voice_answer("T-5", SUMMARY_ID, ["confirm"])
    assert service.get_session("T-5").state == "CONFIRMED"
    worker.join(timeout=5)
    assert not holder["result"].cancelled


def test_voice_relay_without_an_open_session_is_honest(service):
    assert "no open question" in service.relay_voice_answer("T-none", "approach", ["a"]).lower()


def test_confirm_before_answering_is_refused(service):
    flow = _approach_flow()

    def run():
        service.collect("T-6", flow, request="add a plugin")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    _wait_for_session(service, "T-6")
    assert service.confirm("T-6") != ""
    service.cancel("T-6")
    worker.join(timeout=5)


# ── helpers ───────────────────────────────────────────────────────────────────

def _wait_for_session(service, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service.get_session(task_id) is not None:
            return
        time.sleep(0.01)
    raise AssertionError(f"session {task_id} never opened")
