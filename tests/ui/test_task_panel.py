"""Task progress pane + question flow view (offscreen, no HUD).

The pane is the answer surface for the whole feature, so these tests pin the
behaviour the HUD depends on: it renders the REAL registry snapshot, it keeps
its state when hidden, CANCEL and REPORT call the right callbacks, several
tasks never cross-talk, and the question view dispatches every answer to the
brain question service (the panel is authoritative).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .qt_harness import qapp  # noqa: F401,E402

import ui_task_panel as tp  # noqa: E402
from brain import task_questions as tq  # noqa: E402
from brain.task_engine import TaskRegistry  # noqa: E402
from brain.task_questions import QuestionService  # noqa: E402


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(tq, "_gate_enabled", lambda: True)


@pytest.fixture()
def registry(tmp_path):
    return TaskRegistry(db_path=tmp_path / "tasks.sqlite",
                        reports_dir=tmp_path / "reports", emit_logs=False)


def _pane(registry):
    return tp.TaskProgressPane(
        list_provider=lambda: registry.list_tasks(limit=25),
        report_provider=lambda tid: registry.report_text(tid),
    )


def _seed(registry, *, title="Search fusion news", tool="web_search",
          status="SUCCESS", category="SEARCH"):
    tid = registry.begin_request(title, source="text", category=category,
                                 request=title)
    step = registry.note_tool_started(tid, tool, detail="fusion", corr="c1")
    registry.note_tool_result(tid, "c1", status,
                              "found 4 sources" if status == "SUCCESS" else "boom",
                              step_id=step)
    return tid


# ── the pane ──────────────────────────────────────────────────────────────────

def test_pane_renders_the_real_snapshot(qapp, registry):
    tid = _seed(registry)
    pane = _pane(registry)
    pane.apply_event({**registry.snapshot(tid), "event": "step_completed"})
    texts = [pane._status.text(), pane._current.text()]
    assert "RUNNING" in texts[0]
    assert "Search fusion news" in texts[1]
    # The checklist row names the real tool and its outcome.
    rows = [w for w in pane._steps_host.findChildren(tp.QLabel)]
    labels = " ".join(w.text() for w in rows)
    assert "web_search" in labels
    assert "found 4 sources" in labels
    # Internal lifecycle bookkeeping is not presented as a fake repeated checklist.
    assert "Analyze the research request" not in labels
    assert "Deliver response" not in labels
    pane.deleteLater()


def test_task_without_recorded_operations_does_not_get_fake_checklist_rows(qapp, registry):
    tid = registry.begin_request(
        "Open Task Progress Panel", source="text", category="AUTOMATE",
        request="Open Task Progress Panel")
    pane = _pane(registry)
    pane.apply_event({**registry.snapshot(tid), "event": "task_created"})

    labels = " ".join(w.text() for w in pane._steps_host.findChildren(tp.QLabel))
    assert "Analyze the request" not in labels
    assert "Deliver response" not in labels
    assert "Waiting for concrete execution steps" in labels
    pane.deleteLater()


def test_state_survives_hiding_and_reopening(qapp, registry):
    tid = _seed(registry)
    pane = _pane(registry)
    pane.apply_event(registry.snapshot(tid))
    pane.hide()
    pane.refresh()
    assert pane.current_task_id() == tid
    assert "Search fusion news" in pane._current.text()
    pane.deleteLater()


def test_cancel_button_passes_the_selected_task(qapp, registry):
    tid = _seed(registry)
    cancelled: list[str] = []
    pane = tp.TaskProgressPane(list_provider=lambda: registry.list_tasks(),
                               on_cancel=cancelled.append)
    pane.apply_event(registry.snapshot(tid))
    assert pane._cancel_btn.isEnabled()
    pane._cancel_btn.click()
    assert cancelled == [tid]
    pane.deleteLater()


def test_report_button_hands_back_the_real_report(qapp, registry):
    tid = _seed(registry)
    registry.complete(tid, summary="four sources summarised")
    captured: list[tuple] = []
    pane = tp.TaskProgressPane(list_provider=lambda: registry.list_tasks(),
                               report_provider=lambda t: registry.report_text(t),
                               on_report=lambda t, text: captured.append((t, text)))
    pane.apply_event(registry.snapshot(tid))
    assert pane._report_btn.isEnabled()
    pane._report_btn.click()
    assert captured and captured[0][0] == tid
    assert "Task report" in captured[0][1]
    pane.deleteLater()


def test_terminal_task_disables_cancel(qapp, registry):
    tid = _seed(registry)
    registry.cancel(tid, "user stopped it")
    pane = _pane(registry)
    pane.apply_event(registry.snapshot(tid))
    assert not pane._cancel_btn.isEnabled()
    pane.deleteLater()


def test_multiple_tasks_are_listed_without_cross_talk(qapp, registry):
    first = _seed(registry, title="First task")
    second = _seed(registry, title="Second task", tool="file_processor")
    pane = _pane(registry)
    pane.refresh()
    assert pane._selector.count() == 2
    pane.select_task(first)
    assert "First task" in pane._current.text()
    pane.select_task(second)
    assert "Second task" in pane._current.text()
    # Selecting one task never shows the other's steps.
    labels = " ".join(w.text() for w in pane._steps_host.findChildren(tp.QLabel))
    assert "file_processor" in labels
    assert "web_search" not in labels
    pane.deleteLater()


def test_new_task_created_switches_checklist_while_previous_task_is_active(qapp, registry):
    """A new user request must replace the old checklist without waiting for it to finish."""
    first = registry.begin_request(
        "Open Task Progress Panel", source="text", category="AUTOMATE",
        request="Open Task Progress Panel")
    registry.mark_running(first)
    pane = _pane(registry)
    pane.apply_event({**registry.snapshot(first), "event": "task_started"})
    assert pane.current_task_id() == first
    assert "Open Task Progress Panel" in pane._current.text()

    second = registry.begin_request(
        "Scan all your memory", source="text", category="ANALYZE",
        request="Scan all your memory")
    pane.apply_event({**registry.snapshot(second), "event": "task_created"})

    assert pane.current_task_id() == second
    assert "Scan all your memory" in pane._current.text()
    pane.deleteLater()


def test_refresh_follows_newest_active_task_if_create_event_was_missed(qapp, registry):
    first = registry.begin_request("First request", source="text", request="First request")
    registry.mark_running(first)
    pane = _pane(registry)
    pane.apply_event({**registry.snapshot(first), "event": "task_started"})
    assert pane.current_task_id() == first

    second = registry.begin_request("Scan all your memory", source="text",
                                   request="Scan all your memory")
    registry.mark_running(second)
    # The second task event was intentionally not delivered to the pane.
    pane.refresh()

    assert pane.current_task_id() == second
    assert "Scan all your memory" in pane._current.text()
    pane.deleteLater()


# ── the question flow view ────────────────────────────────────────────────────

@pytest.fixture()
def service():
    svc = QuestionService()
    yield svc
    svc.cancel_all("test over")


def _open_flow(monkeypatch, service, request="add a plugin for camera streaming"):
    """Start a real gate collection in a worker thread and return its id."""
    monkeypatch.setattr(tp, "_service", lambda: service)
    flow = tq.build_flow("dev_agent", {"description": request}, request)
    assert flow is not None
    task_id = "TASK-TEST-0001"
    worker = threading.Thread(
        target=lambda: service.collect(task_id, flow, request=request),
        daemon=True)
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline and service.get_session(task_id) is None:
        time.sleep(0.01)
    assert service.get_session(task_id) is not None
    return task_id, worker


def test_question_view_marks_the_recommendation_and_answers(qapp, monkeypatch, service):
    task_id, worker = _open_flow(monkeypatch, service)
    view = tp.QuestionFlowView()
    view.set_snapshot(service.session_snapshot(task_id))
    assert "implementation approach" in view._title.text()
    # The recommended option is marked in the widget text, not pre-selected.
    marked = [b.text() for b in view._checks.values() if b.text().startswith("★")]
    assert marked, "the recommendation must be visible"
    assert not any(b.isChecked() for b in view._checks.values()), \
        "a recommendation is never a selection"

    # Choose the alternative answer and advance.
    view._checks["separate_modules"].setChecked(True)
    view._on_next()
    snapshot = service.session_snapshot(task_id)
    assert snapshot["current_id"] == "files"
    # The next question renders against the same session.
    view.set_snapshot(snapshot)
    assert "files" in view._title.text().lower()
    service.cancel(task_id)
    worker.join(timeout=5)
    view.deleteLater()


def test_question_view_shows_and_confirms_the_summary(qapp, monkeypatch, service):
    task_id, worker = _open_flow(monkeypatch, service)
    view = tp.QuestionFlowView()
    view.set_snapshot(service.session_snapshot(task_id))
    view._checks["reuse_existing"].setChecked(True)
    view._on_next()
    view.set_snapshot(service.session_snapshot(task_id))
    view._checks["named_only"].setChecked(True)
    view._on_next()

    snapshot = service.session_snapshot(task_id)
    assert snapshot["state"] == "SUMMARY"
    view.set_snapshot(snapshot)
    assert "Reuse the existing architecture" in view._summary.text()
    view._on_confirm()
    assert service.get_session(task_id).state == "CONFIRMED"
    worker.join(timeout=5)


def test_question_view_validation_error_is_shown(qapp, monkeypatch, service):
    task_id, worker = _open_flow(monkeypatch, service)
    view = tp.QuestionFlowView()
    view.set_snapshot(service.session_snapshot(task_id))
    view._on_next()                          # nothing selected → refused
    assert view._error.text()
    service.cancel(task_id)
    worker.join(timeout=5)
    view.deleteLater()


def test_question_view_hides_when_the_flow_closes(qapp, monkeypatch, service):
    task_id, worker = _open_flow(monkeypatch, service)
    view = tp.QuestionFlowView()
    view.show()
    view.set_snapshot(service.session_snapshot(task_id))
    assert view.isVisible()
    service.cancel(task_id)
    view.set_snapshot(service.session_snapshot(task_id))
    assert not view.isVisible()
    worker.join(timeout=5)
    view.deleteLater()
