"""Task panel inside the real HUD — layout target, auto-open, cards, answers.

The panel only earns its place if the HUD actually opens it when work starts,
keeps it as a layout-studio target, renders the question card in the chat log,
and routes every answer into the same question service the dispatcher blocks on.
These tests drive a real ``MainWindow`` offscreen.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QUrl

from .test_camera_pipeline import (  # noqa: F401  (fixtures)
    _NullAnalyzer, _pump, camera, qapp, ui,
)


_TASK_SNAPSHOT = {
    "id": "TASK-2026-000001",
    "title": "Search fusion news",
    "state": "RUNNING",
    "source": "voice",
    "category": "SEARCH",
    "elapsed_ms": 1200,
    "cancellable": True,
    "steps": [
        {"id": "analyze", "title": "Analyze the research request",
         "status": "completed", "outcome": "request understood"},
        {"id": "s1", "title": "web_search · fusion", "status": "running",
         "outcome": "", "metadata": {"tool": "web_search"}},
        {"id": "validate", "title": "Validate the sources and findings",
         "status": "pending", "outcome": ""},
    ],
    "answers": {}, "findings": [], "warnings": [], "errors": [],
    "artifacts": [], "questions_state": "pending", "event": "step_started",
}


@pytest.fixture()
def window(qapp, monkeypatch, camera):  # noqa: F811
    """A real HUD whose config and feeds live outside the shared ones."""
    with tempfile.TemporaryDirectory(prefix="jarvis-tasks-") as tmp:
        root = Path(tmp)
        config_dir = root / "config"
        config_dir.mkdir(exist_ok=True)
        config = {"assistant_name": "JARVIS", "user_name": "Operator",
                  "gemini_api_key": "test-key", "os_system": "windows",
                  "features": {"remember_position": True, "floating_windows": True}}
        config_path = config_dir / "api_keys.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setattr(ui, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(ui, "API_FILE", config_path)
        monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(config))
        monkeypatch.setattr(ui._FeatheredCamView, "_save_geom", lambda self: None)
        monkeypatch.setattr(ui._FeatheredCamView, "_load_geom", lambda self: None)
        monkeypatch.setattr(ui.RefinedCommandWindow, "_ensure_vision_analyzer",
                            lambda self: _NullAnalyzer())

        win = ui.MainWindow(str(root / "face.png"))
        qapp.processEvents()
        yield win
        try:
            win._shutdown_camera_threads(timeout=5.0)
            for name in ("_cam_live_lbl", "_cam_preview", "_tasks_panel"):
                child = getattr(win, name, None)
                if child is not None:
                    child.close()
            qapp.processEvents()
            win.close()
        except Exception:
            pass
        qapp.processEvents()


def test_tasks_panel_is_a_layout_target_and_starts_hidden(window):
    assert window._tasks_panel is not None
    assert window._tasks_panel.property("_jarvis_panel_kind") == "tasks"
    assert window._system_layout_widgets["Task Progress"] is window._tasks_panel
    assert window._tasks_panel.isHidden(), "panels stay closed until asked for"


def test_control_jarvis_window_opens_and_closes_the_task_panel(window, qapp):
    assert window.control_jarvis_window("tasks", "open").startswith("Opened")
    _pump(qapp, 0.4)
    assert window._tasks_panel.isVisible()
    assert window.control_jarvis_window("tasks", "close").startswith("Closed")
    _pump(qapp, 0.5)
    assert not window._tasks_panel.isVisible(), "closing only hides the panel"


def test_jobs_alias_opens_the_task_panel(window, qapp):
    window.control_jarvis_window("jobs", "open")
    _pump(qapp, 0.4)
    assert window._tasks_panel.isVisible()


def test_task_alias_still_means_the_web_queue(window, qapp):
    window.control_jarvis_window("task", "open")
    _pump(qapp, 0.4)
    assert window._web_task_panel.isVisible()
    assert not window._tasks_panel.isVisible(), \
        "'task' must keep pointing at the web research queue"


def test_opening_an_visible_panel_refreshes_without_reshowing(window, qapp):
    assert window._open_tasks_panel() is True
    _pump(qapp, 0.2)
    assert window._tasks_panel.isVisible()
    # Second open must not restart the fade / steal focus.
    assert window._open_tasks_panel() is True
    assert window._tasks_panel.isVisible()


def test_task_event_updates_the_pane_and_autopens(window, qapp):
    window._apply_task_event(dict(_TASK_SNAPSHOT))
    _pump(qapp, 0.2)
    assert window._tasks_panel.isVisible(), "a real task auto-opens the panel"
    assert "Search fusion news" in window._tasks_pane._current.text()
    assert window._tasks_pane.current_task_id() == "TASK-2026-000001"


def test_auto_open_respects_the_feature_flag(window, monkeypatch, qapp):
    monkeypatch.setattr(ui.MainWindow, "_task_auto_open_enabled", lambda self: False)
    window._apply_task_event(dict(_TASK_SNAPSHOT))
    _pump(qapp, 0.2)
    assert not window._tasks_panel.isVisible()
    # The panel still learns the task, it just does not force itself on screen.
    assert window._tasks_pane.current_task_id() == "TASK-2026-000001"


def test_cancel_button_reaches_the_backend_callback(window):
    calls: list[str] = []
    window.on_task_cancel = calls.append
    window._apply_task_event(dict(_TASK_SNAPSHOT))
    window._cancel_task_from_panel("TASK-2026-000001")
    assert calls and "TASK-2026-000001" in calls[0]


def test_report_renders_in_the_task_panel_card(window, monkeypatch):
    """The report belongs in the checklist card, not a second content surface.

    ``_show_task_report`` renders into the task pane's own card ("in the
    existing checklist card only") so the report stays attached to the
    checklist it explains. This test used to pin the older show_content()
    route; the committed implementation replaced that, and the pin is updated
    to the contract that actually ships.
    """
    captured: list[str] = []
    pane = window._tasks_pane
    monkeypatch.setattr(pane, "show_report_details",
                        lambda text: captured.append(str(text)))

    window._show_task_report("TASK-2026-000001", "# Task report\n\nreal data")

    assert captured == ["# Task report\n\nreal data"]


def test_an_empty_report_still_renders_an_honest_placeholder(window, monkeypatch):
    captured: list[str] = []
    pane = window._tasks_pane
    monkeypatch.setattr(pane, "show_report_details",
                        lambda text: captured.append(str(text)))

    window._show_task_report("TASK-2026-000001", "")

    assert captured and "No report is available" in captured[0]
    assert "TASK-2026-000001" in captured[0]


def test_question_card_renders_clickable_options(window, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(window, "_append_chat", lambda html: captured.append(html))
    window._post_question_card({
        "task_id": "TASK-2026-000001", "state": "ASKING",
        "question": {
            "id": "approach", "title": "What implementation approach should I use?",
            "explanation": "Decides the structure.", "required": True,
            "allow_custom": True, "multi_select": False,
            "recommended_option": "reuse_existing",
            "recommended_reason": "the request names existing code",
            "options": [
                {"id": "reuse_existing", "label": "Reuse the existing architecture"},
                {"id": "separate_modules", "label": "Create separate modules"},
            ],
        },
    })
    assert captured
    html = captured[0]
    assert "CLARIFICATION NEEDED" in html
    assert "jarvis-answer:TASK-2026-000001:approach:reuse_existing" in html
    assert "jarvis-answer:TASK-2026-000001:approach:separate_modules" in html
    assert "recommended" in html


def test_summary_card_offers_confirm_and_cancel(window, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(window, "_append_chat", lambda html: captured.append(html))
    window._post_question_card({
        "task_id": "TASK-2026-000009", "state": "SUMMARY",
        "summary": "- Approach → Reuse the existing architecture",
    })
    html = captured[0]
    assert "CONFIRM THE PLAN" in html
    assert "jarvis-answer:TASK-2026-000009:summary:confirm" in html
    assert "jarvis-answer:TASK-2026-000009:summary:cancel" in html


def test_chat_anchor_answers_the_real_question_service(qapp, monkeypatch):
    from brain import task_questions as tq
    from brain.task_questions import get_question_service, reset_question_service

    monkeypatch.setattr(tq, "_gate_enabled", lambda: True)
    service = get_question_service()
    request = "add a plugin for camera streaming"
    flow = tq.build_flow("dev_agent", {"description": request}, request)
    assert flow is not None
    task_id = "TASK-ANCHOR-0001"
    worker = threading.Thread(
        target=lambda: service.collect(task_id, flow, request=request), daemon=True)
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline and service.get_session(task_id) is None:
        time.sleep(0.01)
    assert service.get_session(task_id) is not None

    browser = ui.MarkdownTextBrowser()
    browser._on_anchor_clicked(
        QUrl(f"jarvis-answer:{task_id}:approach:separate_modules"))
    assert service.session_snapshot(task_id)["current_id"] == "files"

    service.cancel(task_id, "test over")
    worker.join(timeout=5)
    browser.deleteLater()
    reset_question_service()
