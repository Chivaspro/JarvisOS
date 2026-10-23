"""Dispatcher integration — task lifecycle, the gate, and turn settlement.

These tests drive the REAL ``JarvisLive`` helper methods (unbound, on a light
stub) exactly as ``_execute_tool`` and ``_turn_complete`` call them, with the
registry and question service swapped for isolated instances. They cover the
delivery scenarios end to end: a task with no questions, a task that requires
clarification, recommended/other/custom answers, cancellation, failure, and
report generation.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as jarvis_main  # noqa: E402
from brain import task_questions as tq  # noqa: E402
from brain.task_engine import TaskRegistry  # noqa: E402
from brain.task_questions import QuestionService  # noqa: E402


# ── harness ───────────────────────────────────────────────────────────────────

_BOUND = (
    "_begin_turn_task", "_current_turn_task", "_clear_turn_task",
    "_task_for_dispatch", "_request_text_for_task", "_note_task_step_started",
    "_note_task_result", "_question_gate", "_handle_question_answer",
    "_settle_turn_task", "_cancel_turn_task", "_cancel_current_task",
    "_on_task_event", "_on_orchestrator_state",
)


class _Harness:
    for _name in _BOUND:
        locals()[_name] = getattr(jarvis_main.JarvisLive, _name)
    del _name


class _FakeUI:
    def __init__(self):
        self.muted = False
        self.logs: list[str] = []
        self.panels: list[dict] = []
        self.cards: list[dict] = []
        self.events: list[dict] = []

    def write_log(self, text):
        self.logs.append(str(text))

    def set_state(self, state):
        pass

    def clear_doing(self, category):
        pass

    def clear_chat_activity(self):
        pass

    def show_question_flow(self, snapshot):
        self.panels.append(snapshot)

    def show_question_card(self, snapshot):
        self.cards.append(snapshot)

    def task_event(self, payload):
        self.events.append(payload)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    registry = TaskRegistry(db_path=tmp_path / "tasks.sqlite",
                            reports_dir=tmp_path / "reports", emit_logs=False)
    service = QuestionService()
    monkeypatch.setattr(jarvis_main, "_task_registry", lambda: registry)
    monkeypatch.setattr(jarvis_main, "_question_service", lambda: service)
    monkeypatch.setattr(tq, "_gate_enabled", lambda: True)
    h = _Harness()
    h.ui = _FakeUI()
    h._task_lock = threading.RLock()
    h._turn_task_id = None
    h._live_user_utterance = ""
    h._last_user_utterance = ""
    h.interrupt_calls = []
    h.interrupt = lambda source="manual": h.interrupt_calls.append(source)
    # Mirror the production wiring in JarvisLive.run(): the panel answers
    # through the service, and registry events reach the UI.
    service.bind(show_panel=h.ui.show_question_flow,
                 show_card=h.ui.show_question_card,
                 log=h.ui.write_log)
    registry.add_listener(h._on_task_event)
    return h, registry, service, tmp_path


def _fc(name):
    return SimpleNamespace(id=f"fc-{name}", name=name)


def _answer_flow(service, task_id, approach="reuse_existing", custom=""):
    service.resolve_from_panel(task_id, "approach", [approach], custom=custom)
    session = service.get_session(task_id)
    if session is not None and session.current_id == "files":
        service.resolve_from_panel(task_id, "files", ["named_only"])
    service.confirm(task_id)


async def _drive_gate(harness, service, task_id, tool, args, action):
    """Run the gate while a driver answers from the same event loop."""
    gate = asyncio.ensure_future(harness._question_gate(tool, args, _fc(tool), task_id))
    for _ in range(600):
        if service.get_session(task_id) is not None:
            break
        await asyncio.sleep(0.005)
    action(service, task_id)
    return await gate


# ── task creation & step recording ────────────────────────────────────────────

def test_typed_request_opens_a_task_record(env):
    h, registry, _svc, _tmp = env
    task_id = h._begin_turn_task("research quantum error correction", source="text")
    assert task_id.startswith("TASK-")
    assert h._current_turn_task() == task_id
    assert registry.get(task_id).state == "CREATED"


def test_tool_dispatch_creates_and_closes_a_step(env):
    h, registry, _svc, _tmp = env
    task_id = h._begin_turn_task("search the web for fusion news")
    step_id = h._note_task_step_started(task_id, "web_search", {"query": "fusion"}, "c1")
    assert step_id
    h._note_task_result(task_id, "web_search", {"query": "fusion"},
                        "Found 4 sources about fusion", "SUCCESS", "c1", step_id)
    rec = registry.get(task_id)
    tool_step = next(s for s in rec.steps if s.metadata.get("tool"))
    assert tool_step.status == "completed"
    assert rec.state == "RUNNING"


def test_failed_tool_fails_its_step(env):
    h, registry, _svc, _tmp = env
    task_id = h._begin_turn_task("open the app")
    step_id = h._note_task_step_started(task_id, "pc_control", {"action": "open"}, "c2")
    h._note_task_result(task_id, "pc_control", {"action": "open"},
                        "could not open that app", "FAILED", "c2", step_id)
    rec = registry.get(task_id)
    assert any(s.status == "failed" for s in rec.steps)
    assert rec.errors


def test_answer_only_turn_is_recorded_as_a_completed_task(env):
    h, registry, _svc, tmp = env
    h._last_user_utterance = "what is quantum entanglement?"
    h._settle_turn_task("what is quantum entanglement?", "It is a physical phenomenon.")
    tasks = registry.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["state"] == "COMPLETED"
    assert (tmp / "reports" / f"{tasks[0]['id']}.md").exists()


def test_pure_greeting_is_never_recorded(env):
    h, registry, _svc, _tmp = env
    h._last_user_utterance = "hello jarvis"
    h._settle_turn_task("hello jarvis", "Good afternoon, sir.")
    assert registry.list_tasks() == []


# ── the question gate ─────────────────────────────────────────────────────────

def test_clear_request_is_not_gated(env):
    h, registry, service, _tmp = env
    task_id = h._begin_turn_task("reuse the existing parser in parser.py")
    h._live_user_utterance = "reuse the existing parser in parser.py"
    response = asyncio.run(h._question_gate("dev_agent", {"description": "reuse parser"},
                                            _fc("dev_agent"), task_id))
    assert response is None, "a clear request must execute immediately"
    assert registry.get(task_id).questions_state == "none_needed"


def test_material_call_is_held_until_the_user_confirms(env):
    h, registry, service, _tmp = env
    h._live_user_utterance = "add a plugin that streams camera frames"
    task_id = h._begin_turn_task(h._live_user_utterance)
    response = asyncio.run(_drive_gate(
        h, service, task_id, "dev_agent", {"description": "add a plugin"},
        lambda svc, tid: _answer_flow(svc, tid, "reuse_existing")))
    assert response is not None
    assert "[USER_DECISIONS]" in response.response["result"]
    assert "Reuse the existing architecture" in response.response["result"]
    rec = registry.get(task_id)
    assert rec.questions_state == "answered"
    assert rec.answers["approach"] == "Reuse the existing architecture"
    assert h.ui.panels and h.ui.cards, "the panel and a chat card were shown"


def test_gate_only_evaluates_once_per_task(env):
    h, registry, service, _tmp = env
    h._live_user_utterance = "add a plugin that streams camera frames"
    task_id = h._begin_turn_task(h._live_user_utterance)
    asyncio.run(_drive_gate(h, service, task_id, "dev_agent",
                            {"description": "add a plugin"}, _answer_flow))
    # A second material call in the same task is never questioned again.
    second = asyncio.run(h._question_gate("documents", {"instruction": "write it up"},
                                          _fc("documents"), task_id))
    assert second is None


def test_non_material_tool_is_never_gated(env):
    h, registry, service, _tmp = env
    task_id = h._begin_turn_task("save this fact")
    h._live_user_utterance = "save this fact"
    response = asyncio.run(h._question_gate("save_memory", {"key": "x"}, 
                                            _fc("save_memory"), task_id))
    assert response is None


def test_cancelled_gate_marks_the_task_cancelled(env):
    h, registry, service, _tmp = env
    h._live_user_utterance = "add a plugin that streams camera frames"
    task_id = h._begin_turn_task(h._live_user_utterance)
    response = asyncio.run(_drive_gate(
        h, service, task_id, "dev_agent", {"description": "add a plugin"},
        lambda svc, tid: svc.cancel(tid, "the user interrupted")))
    assert response is not None
    assert "[CLARIFICATION_CANCELLED]" in response.response["result"]
    assert registry.get(task_id).state == "CANCELLED"
    assert h._current_turn_task() == ""


def test_alternative_and_custom_answers_reach_the_model(env):
    h, registry, service, _tmp = env
    h._live_user_utterance = "add a plugin for camera streaming"
    task_id = h._begin_turn_task(h._live_user_utterance)
    response = asyncio.run(_drive_gate(
        h, service, task_id, "dev_agent", {"description": "add a plugin"},
        lambda svc, tid: _answer_flow(svc, tid, "other",
                                      custom="a standalone script")))
    text = response.response["result"]
    assert "[USER_DECISIONS]" in text
    assert "a standalone script" in text
    assert registry.get(task_id).answers["approach"].endswith('"a standalone script"')


def test_voice_relay_then_confirm_through_the_tool(env):
    h, registry, service, _tmp = env
    h._live_user_utterance = "add a plugin for camera streaming"
    task_id = h._begin_turn_task(h._live_user_utterance)

    async def _drive():
        gate = asyncio.ensure_future(h._question_gate(
            "dev_agent", {"description": "add a plugin"}, _fc("dev_agent"), task_id))
        for _ in range(600):
            if service.get_session(task_id) is not None:
                break
            await asyncio.sleep(0.005)
        # The model relays the spoken answers through the tool.
        h._handle_question_answer({"question_id": "approach",
                                   "option_ids": ["separate_modules"],
                                   "utterance": "make it a separate module"})
        h._handle_question_answer({"question_id": "files",
                                   "option_ids": ["named_only"],
                                   "utterance": "only the plugin file"})
        h._handle_question_answer({"question_id": "summary",
                                   "option_ids": ["confirm"]})
        return await gate

    response = asyncio.run(_drive())
    assert "[USER_DECISIONS]" in response.response["result"]
    assert "Create separate modules" in response.response["result"]
    detail = registry.get(task_id).answers
    assert "Create separate modules" in detail["approach"]


# ── settlement, cancellation, isolation ───────────────────────────────────────

def test_settle_fails_the_turn_when_every_step_failed(env):
    h, registry, _svc, _tmp = env
    task_id = h._begin_turn_task("open the app")
    step_id = h._note_task_step_started(task_id, "pc_control", {"action": "open"}, "c3")
    h._note_task_result(task_id, "pc_control", {"action": "open"}, "not installed",
                        "FAILED", "c3", step_id)
    h._settle_turn_task("open the app", "")
    assert registry.get(task_id).state == "FAILED"


def test_settle_completes_a_successful_turn(env):
    h, registry, _svc, tmp = env
    task_id = h._begin_turn_task("search for fusion news")
    step_id = h._note_task_step_started(task_id, "web_search", {"query": "fusion"}, "c4")
    h._note_task_result(task_id, "web_search", {"query": "fusion"}, "4 sources",
                        "SUCCESS", "c4", step_id)
    h._settle_turn_task("search for fusion news", "Here is what I found.")
    rec = registry.get(task_id)
    assert rec.state == "COMPLETED"
    assert (tmp / "reports" / f"{task_id}.md").exists()
    assert h._current_turn_task() == ""


def test_interrupt_cancels_the_task_and_keeps_partial_results(env):
    h, registry, _svc, _tmp = env
    task_id = h._begin_turn_task("research something")
    step_id = h._note_task_step_started(task_id, "web_search", {"query": "q"}, "c5")
    h._note_task_result(task_id, "web_search", {"query": "q"}, "3 sources",
                        "SUCCESS", "c5", step_id)
    h._cancel_turn_task("the turn was interrupted")
    rec = registry.get(task_id)
    assert rec.state == "CANCELLED"
    assert any("3 sources" in f for f in rec.findings), "partial results preserved"


def test_cancel_current_task_releases_the_gate_and_interrupts(env):
    h, registry, service, _tmp = env
    h._live_user_utterance = "add a plugin for camera streaming"
    task_id = h._begin_turn_task(h._live_user_utterance)

    async def _drive():
        gate = asyncio.ensure_future(h._question_gate(
            "dev_agent", {"description": "add a plugin"}, _fc("dev_agent"), task_id))
        for _ in range(600):
            if service.get_session(task_id) is not None:
                break
            await asyncio.sleep(0.005)
        message = h._cancel_current_task("cancelled from the panel")
        return message, await gate

    message, response = asyncio.run(_drive())
    assert "Cancelled" in message
    assert response is not None and "[CLARIFICATION_CANCELLED]" in response.response["result"]
    assert registry.get(task_id).state == "CANCELLED"
    assert h.interrupt_calls == ["manual"]


def test_two_sequential_turns_never_cross_talk(env):
    h, registry, _svc, _tmp = env
    first = h._begin_turn_task("first request")
    h._settle_turn_task("first request", "first answer")
    second = h._begin_turn_task("second request")
    h._settle_turn_task("second request", "second answer")
    recs = {t["id"]: t for t in registry.list_tasks()}
    assert recs[first]["title"] == "first request"
    assert recs[second]["title"] == "second request"
    assert recs[first]["state"] == recs[second]["state"] == "COMPLETED"


def test_task_events_reach_the_ui(env):
    h, registry, _svc, _tmp = env
    task_id = h._begin_turn_task("watch the events")
    h._settle_turn_task("watch the events", "done")
    assert any(e.get("id") == task_id for e in h.ui.events)
    assert any(e.get("event") == "task_completed" for e in h.ui.events)


# ── artifacts ─────────────────────────────────────────────────────────────────

def test_artifacts_only_come_from_file_producing_tools():
    assert jarvis_main._task_artifacts_from_result(
        "system_status", {}, "CPU 12%") == []
    found = jarvis_main._task_artifacts_from_result(
        "documents", {"output_path": "C:/out/report.docx"}, "Saved C:/out/report.docx")
    assert any("report.docx" in p for p in found)
