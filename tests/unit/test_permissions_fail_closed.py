"""The permission gate must fail CLOSED (master-prompt defect 3).

Before this fix an unreadable permission switch read as "enabled": the helper
swallowed the exception and returned "not off", so a broken feature lookup — or
a destroyed UI window, whose inert stand-in answers ``True`` for every switch —
silently authorised desktop, terminal, file, camera and account actions.

These tests pin the new contract:

* only a definite "on" authorises an action;
* a switch that cannot be read is treated as REVOKED, with its own
  non-sensitive explanation (never the raw exception text, never a secret);
* the explicitly read-only probes (status / list / engines) answer without
  consulting the gate, so diagnostics keep working;
* the approval service and the main dispatcher wrapper share the contract;
* the UI proxy never reads a dead window as "everything enabled".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from security.permissions import permission_refusal  # noqa: E402
from security import approvals  # noqa: E402


def _all_on(_name):
    return True


def _all_off(_name):
    return False


def _raises(_name):
    raise RuntimeError("config unreadable")


#: (tool, args) pairs where a Settings switch is consulted for the action.
GATED = [
    ("system_status", {}),
    ("file_controller", {"action": "read"}),
    ("file_processor", {"action": "read"}),
    ("code_helper", {"action": "run"}),
    ("dev_agent", {"action": "build"}),
    ("screen_process", {}),
    ("vision_look", {}),
    ("recognize_faces", {}),
    ("what_am_i_holding", {}),
    ("detect_objects", {}),
    ("detect_gesture", {}),
    ("voice_settings", {"mode": "set"}),
    ("diagnostics", {"mode": "check"}),
    ("computer_control", {"action": "click"}),
    ("browser_control", {}),
    ("web_research", {}),
    ("pc_control", {"action": "click"}),
    ("visual_control", {}),
    ("browser_action", {}),
    ("power_control", {"action": "shutdown"}),
    ("system_control", {"action": "list_processes"}),
    ("window_control", {"action": "minimize"}),
    ("clipboard_sentry", {"action": "write"}),
    ("documents", {"action": "create"}),
    ("globe", {"action": "place"}),
    ("smart_home", {"action": "toggle"}),
    ("instagram", {"action": "post"}),
    ("live_vision", {}),
]

#: Read-only probes that must answer WITHOUT consulting the gate.
READ_ONLY = [
    ("voice_settings", {"mode": "status"}),
    ("documents", {"action": "engines"}),
    ("globe", {"action": "list"}),
    ("globe", {"action": "status"}),
    ("instagram", {"action": "status"}),
]


# ── the core contract ────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,args", GATED)
def test_a_readable_off_switch_refuses_and_names_it(tool, args):
    refusal = permission_refusal(_all_off, tool, args)
    assert refusal, f"{tool} was not refused while its switch was off"
    assert "switched off in Settings" in refusal


@pytest.mark.parametrize("tool,args", GATED)
def test_a_readable_on_switch_allows(tool, args):
    assert permission_refusal(_all_on, tool, args) is None, tool


@pytest.mark.parametrize("tool,args", GATED)
def test_an_unreadable_switch_fails_closed(tool, args):
    refusal = permission_refusal(_raises, tool, args)
    assert refusal, f"{tool} was ALLOWED while its permission could not be read"
    assert "could not read" in refusal
    # Non-sensitive: the underlying exception text must never leak.
    assert "RuntimeError" not in refusal
    assert "config unreadable" not in refusal


@pytest.mark.parametrize("tool,args", READ_ONLY)
def test_read_only_probes_answer_even_when_the_switch_is_unreadable(tool, args):
    assert permission_refusal(_raises, tool, args) is None
    assert permission_refusal(_all_off, tool, args) is None


def test_an_ungated_tool_is_untouched_by_a_broken_lookup():
    # web_search has no Settings switch: a broken lookup must not invent one.
    assert permission_refusal(_raises, "web_search", {}) is None


# ── malformed arguments must never disable an always-gated tool ──────────────

@pytest.mark.parametrize("tool", ["file_controller", "browser_control",
                                  "screen_process", "live_vision", "smart_home"])
@pytest.mark.parametrize("args", [None, {}, {"action": None}, {"action": 123},
                                  {"action": ["read"]}])
def test_malformed_args_cannot_disable_a_gate(tool, args):
    assert permission_refusal(_all_off, tool, args), (tool, args)
    assert permission_refusal(_raises, tool, args), (tool, args)


# ── the approval service shares the contract ─────────────────────────────────

def test_approvals_evaluate_refuses_when_the_lookup_raises():
    decision = approvals.evaluate("code_helper", {"action": "run"}, enabled=_raises)
    assert decision["allowed"] is False
    assert decision["needs_ui_token"] is False
    assert "could not" in (decision["refusal"] or "")


def test_approvals_evaluate_still_refuses_a_readably_off_switch():
    decision = approvals.evaluate("file_controller", {"action": "read"},
                                  enabled=_all_off)
    assert decision["allowed"] is False and "File control" in decision["refusal"]


def test_approvals_check_permission_refuses_when_the_lookup_raises():
    refusal = approvals.check_permission(_raises, "file_controller",
                                         {"action": "read"})
    assert refusal and "could not" in refusal


def test_dispatcher_permission_gate_refuses_when_the_lookup_raises():
    import main as m

    refusal = m._permission_gate(_raises, "file_controller", {"action": "read"})
    assert refusal and "could not" in refusal
    # A healthy lookup still behaves exactly as before.
    assert m._permission_gate(_all_on, "file_controller", {"action": "read"}) is None


# ── the UI proxy must not read a dead window as "everything enabled" ─────────

def test_ui_proxy_reads_the_live_window_cache():
    from ui import JarvisUI

    proxy = object.__new__(JarvisUI)
    proxy._ui_gone = False

    class _Win:
        _features = {"file_control": False, "smart_home": True}

    proxy._win = _Win()
    assert proxy.feature_enabled("file_control") is False
    assert proxy.feature_enabled("smart_home") is True


def test_ui_proxy_fails_closed_on_a_dead_window(monkeypatch):
    from config import settings

    from ui import JarvisUI, _InertMainWindow

    proxy = object.__new__(JarvisUI)
    proxy._ui_gone = True
    proxy._win = _InertMainWindow()      # empty feature map: used to read True

    monkeypatch.setattr(settings, "feature_enabled", _all_off)
    assert proxy.feature_enabled("desktop_control") is False
    assert proxy.feature_enabled("terminal_control") is False

    monkeypatch.setattr(settings, "feature_enabled", _all_on)
    assert proxy.feature_enabled("desktop_control") is True

    def _boom(_name):
        raise RuntimeError("store gone")

    monkeypatch.setattr(settings, "feature_enabled", _boom)
    assert proxy.feature_enabled("desktop_control") is False


def test_ui_proxy_falls_back_when_the_cache_read_raises(monkeypatch):
    from config import settings

    from ui import JarvisUI

    proxy = object.__new__(JarvisUI)
    proxy._ui_gone = False

    class _BrokenWin:
        _features = None                 # .get() on None raises

    proxy._win = _BrokenWin()
    monkeypatch.setattr(settings, "feature_enabled", _all_off)
    assert proxy.feature_enabled("camera_access") is False
