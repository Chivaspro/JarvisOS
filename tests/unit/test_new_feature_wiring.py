"""Wiring for the four first-party additions: library, meetings, HA and GitHub.

A module that exists but is not declared, not dispatched, not switched or not
described has only the appearance of a feature. These tests are the join
between the module tests and the app: every tool is declared to the model,
every declaration has a dispatch branch and an activity mapping, every gate is
in the config and in the Settings window, and every service row's probe really
resolves.

The permission shape is asserted here too, because it is policy rather than
behavior: while a switch is off, the read-only probes that keep the feature
manageable stay open (``library status``, ``meeting_notes stop/status``) and
everything else is refused before a runner is reached.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from capabilities import actions, registry, router
from config.settings import FEATURE_DEFAULTS
from security.permissions import label_for
from services import hub

PROJECT = Path(__file__).resolve().parents[2]

NEW_TOOLS = {
    "library": "library",
    "meeting_notes": "meeting_notes",
    "home_assistant": "home_assistant",
    "github": "github",
}

_CATEGORIES = {"SEARCH", "CODE", "REVIEW", "VIEW", "FIND", "GENERATE",
               "ANALYZE", "AUTOMATE"}


def _main_source() -> str:
    return (PROJECT / "main.py").read_text(encoding="utf-8")


# ── declaration, dispatch, activity ──────────────────────────────────────────

def test_every_tool_is_declared_to_the_model():
    tree = ast.parse(_main_source())
    declared = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "name"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    declared.add(value.value)
    missing = sorted(name for name in NEW_TOOLS if name not in declared)
    assert not missing, f"not declared in main.py: {missing}"


def test_every_tool_is_dispatched_through_its_own_handler():
    source = _main_source()
    for name in NEW_TOOLS:
        assert f'"{name}": _local_actions.handle' in source, name


def test_each_tool_has_an_activity_mapping_in_a_real_category():
    import main

    samples = {
        "library": {"action": "search", "query": "contract"},
        "meeting_notes": {"action": "start"},
        "home_assistant": {"action": "turn_on", "entity": "light.office"},
        "github": {"action": "issues", "repo": "octocat/hello"},
    }
    for tool, args in samples.items():
        category, detail = main._activity_for_tool(tool, args)
        assert category in _CATEGORIES, f"{tool} has no dashboard category"
        assert detail, f"{tool} shows an empty activity row"


# ── registry, router and runners agree ───────────────────────────────────────

def test_every_new_tool_has_a_runner_a_lock_a_label_and_an_engine():
    for tool in NEW_TOOLS:
        assert tool in registry.TOOLS
        assert registry.names(tool), f"{tool} declares no actions"
        assert tool in actions._RUNNERS, f"{tool} has no runner"
        assert tool in router.ENGINE_OF and tool in router.LOCK_OF \
            and tool in router.LABEL_OF, tool


def test_every_declared_action_reaches_the_module():
    """A declared action the module does not implement would only fail at
    runtime, in front of the user."""
    from capabilities import github, home_assistant, meeting_notes
    from knowledge import library

    modules = {"library": library, "meeting_notes": meeting_notes,
               "home_assistant": home_assistant, "github": github}
    for tool, module in modules.items():
        declared = set(registry.names(tool))
        implemented = set(module._HANDLERS)
        assert declared == implemented, (tool, declared ^ implemented)


def test_the_switches_all_exist_and_meetings_defaults_to_off():
    for switch in NEW_TOOLS.values():
        assert switch in FEATURE_DEFAULTS, switch
    assert FEATURE_DEFAULTS["meeting_notes"] is False
    assert FEATURE_DEFAULTS["library"] is True


def test_every_switch_has_a_human_label():
    for switch in NEW_TOOLS.values():
        label = label_for(switch)
        assert label and label != switch
        assert "_" not in label, f"{switch} fell back to its key"


# ── the permission shape ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,args", [
    ("library", {"action": "ingest", "path": "C:/docs"}),
    ("library", {"action": "search", "query": "x"}),
    ("library", {"action": "read", "doc_id": "abc"}),
    ("library", {"action": "remove", "doc_id": "abc"}),
    ("meeting_notes", {"action": "start"}),
    ("meeting_notes", {"action": "list"}),
    ("meeting_notes", {"action": "transcript"}),
    ("meeting_notes", {"action": "delete", "meeting_id": "20260101-000000-abcd"}),
    ("home_assistant", {"action": "status"}),
    ("home_assistant", {"action": "turn_on", "entity": "light.office"}),
    ("github", {"action": "status"}),
    ("github", {"action": "issues", "repo": "octocat/hello"}),
])
def test_a_switched_off_permission_refuses_before_the_runner(tool, args):
    out = actions._handle(tool, args, enabled=lambda name: False)
    assert "status=NOT_DONE" in out
    assert "switched off in Settings" in out


@pytest.mark.parametrize("tool,args", [
    # The library's status is a local count; it answers so the user can see
    # what is indexed before deciding to switch the library on.
    ("library", {"action": "status"}),
    # A live recording must always be stoppable, and its state must be
    # inspectable, even after the switch was turned off mid-meeting.
    ("meeting_notes", {"action": "status"}),
    ("meeting_notes", {"action": "stop"}),
])
def test_the_manageability_actions_still_answer_while_off(tool, args):
    out = actions._handle(tool, args, enabled=lambda name: False)
    assert "switched off in Settings" not in out


def test_library_ingest_also_needs_file_control():
    # Ingestion reads files, so it needs both switches; with file control
    # revoked the refusal names that switch, not the library.
    out = actions._handle("library", {"action": "ingest", "path": "C:/docs"},
                          enabled=lambda name: name != "file_control")
    assert "status=NOT_DONE" in out
    assert "file_control" in out or "File control" in out


def test_the_confirm_gates_are_declared_where_they_matter():
    assert registry.get("meeting_notes", "start").gate == "confirm"
    assert registry.get("meeting_notes", "delete").gate == "confirm"
    assert registry.get("home_assistant", "run_script").gate == "confirm"
    # Reads and reversible controls must NOT wait for the HUD, or every turn
    # turns into a confirmation dialog.
    for tool, action in (("library", "ingest"), ("library", "remove"),
                         ("home_assistant", "turn_on"),
                         ("home_assistant", "activate_scene"),
                         ("github", "issues"), ("meeting_notes", "stop")):
        assert registry.get(tool, action).gate == "", (tool, action)


def test_running_a_script_is_parked_with_its_own_explanation(monkeypatch):
    from capabilities import actions as act
    from capabilities import home_assistant
    from security import confirm

    parked: dict = {}

    def fake_request(key="", title="", detail="", run=None):
        parked.update({"detail": detail, "run": run})
        return "[CONFIRMATION_PENDING] waiting for the HUD"

    monkeypatch.setattr(confirm, "request", fake_request)
    monkeypatch.setattr(home_assistant, "run",
                        lambda *a, **k: pytest.fail("a script must not run first"))
    out = act._handle("home_assistant",
                      {"action": "run_script", "entity": "script.goodnight"},
                      enabled=lambda name: True)
    assert "CONFIRMATION_PENDING" in out
    assert "can change several devices at once" in parked["detail"]


# ── the app's own surfaces describe the tools ────────────────────────────────

def test_every_new_service_row_probes_something_real():
    ids = {service.id for service in hub.SERVICES}
    for service_id in ("github", "home_assistant", "library", "meeting_notes"):
        assert service_id in ids, service_id
    for service in hub.SERVICES:
        if service.id in ("github", "home_assistant", "library", "meeting_notes"):
            assert service.label and service.summary and service.note
            assert service.probe_name
            assert callable(getattr(hub, service.probe_name, None)), service.id
            assert service.switch in FEATURE_DEFAULTS, service.id


def test_the_settings_window_offers_the_new_switches():
    source = (PROJECT / "ui_settings.py").read_text(encoding="utf-8")
    for switch in NEW_TOOLS.values():
        assert f'"{switch}"' in source, f"{switch} is missing from Settings"


def test_the_installer_offers_the_optional_packages_by_name():
    source = (PROJECT / "app" / "installer.py").read_text(encoding="utf-8")
    assert "markitdown[pdf,docx,xlsx,pptx]" in source
    assert "PyAudioWPatch" in source
    assert "faster-whisper" in source


def test_setup_can_collect_the_new_credentials():
    source = (PROJECT / "setup_jarvis.py").read_text(encoding="utf-8")
    for field in ("github_token", "home_assistant_url", "home_assistant_token"):
        assert f'"{field}"' in source, field


def test_wide_exception_paths_never_take_down_the_app():
    """Every new module must import cleanly on a machine with none of the
    optional packages: a hard import would flash the app and vanish."""
    import importlib

    for name in ("capabilities.github", "capabilities.home_assistant",
                 "capabilities.meeting_notes", "knowledge.library"):
        importlib.import_module(name)
