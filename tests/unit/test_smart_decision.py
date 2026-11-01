"""Smart Decision — deterministic rules, then Laya, then the vocabulary.

The requirement behind this layer: a spoken command with exactly one mechanical
meaning must be routed by a fixed rule, never by a classifier's guess. The local
Laya model is a 322M-parameter System-1 classifier and it DOES misroute commands
it has never seen — the live probes recorded "set chrome priority high" → CODE
and "minimize all windows" → CODE. Smart Decision answers that by deciding in
three layers and always naming which layer decided:

    1. ``quick()`` / the rule table — deterministic, testable, free;
    2. Laya — for everything the rules do not cover;
    3. the built-in vocabulary — the honest fallback.

These tests pin the rule table, the guards that keep conversational sentences
out of it, and the fusion order (a rule ALWAYS beats a Laya category).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capabilities import decide, registry  # noqa: E402

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def _state(*, desktop=True, playwright=True, uitars=True, window=True,
           files=True, clipboard=True, ocr=True, uia=True) -> dict:
    """Facts shaped exactly like ``decide.facts()`` returns them."""
    return {
        "media": {"available": True, "installed": {"spotify": r"C:\apps\spotify.exe"},
                  "preferred": "spotify", "detail": "probed"},
        "controllers": {
            "desktop": {"usable": desktop, "detail": "ready" if desktop else "missing"},
            "playwright": {"usable": playwright, "detail": "ready"},
            "uitars_visual": {"usable": uitars, "detail": "ready"},
            "power_control": {"usable": True, "detail": "ready"},
            "window_control": {"usable": window, "detail": "ready"},
            "file_control": {"usable": files, "detail": "ready"},
            "clipboard": {"usable": clipboard, "detail": "ready"},
            "ocr": {"usable": ocr, "detail": "ready"},
            "uia": {"usable": uia, "detail": "ready"},
        },
        "agent_tars": {"usable": False, "detail": "node missing"},
        "agent_reach": {"available": False, "detail": "missing"},
        "switches": {name: True for name in decide._SWITCHES},
    }


# ── the deterministic rule table ─────────────────────────────────────────────

@pytest.mark.parametrize("spoken,intent,tool,action", [
    # window arrangement (Laya misrouted this to CODE in live probes)
    ("minimize all windows", "window", "window_control", "minimize_all"),
    ("minimise every window", "window", "window_control", "minimize_all"),
    ("tile all windows", "window", "window_control", "arrange_all"),
    ("list the open windows", "window", "window_control", "list_windows"),
    ("list the monitors", "window", "window_control", "list_monitors"),
    ("tile chrome to the left", "window", "window_control", "tile_left"),
    ("snap notepad to the top-right corner", "window", "window_control", "snap_grid"),
    ("move spotify to monitor 2", "window", "window_control", "move_to_monitor"),
    # system control (Laya misrouted "set chrome priority high" to CODE)
    ("set chrome priority high", "system", "system_control", "set_process_priority"),
    ("set notepad priority to above normal", "system", "system_control",
     "set_process_priority"),
    ("list the running processes", "system", "system_control", "list_processes"),
    ("kill chrome", "system", "system_control", "kill_process"),
    ("turn wifi off", "system", "system_control", "wifi_on"),
    ("is wifi on", "system", "system_control", "wifi_status"),
    ("set volume to 40", "system", "system_control", "set_volume"),
    ("set brightness to 70", "system", "system_control", "set_brightness"),
    ("switch to dark theme", "system", "system_control", "set_theme"),
    ("what's using my cpu", "system", "system_control", "system_metrics"),
    # screen reading (deterministic, no model)
    ("take a screenshot", "screen", "pc_control", "screen_screenshot"),
    ("read the screen", "screen", "pc_control", "read_screen"),
    ("what's on my screen", "screen", "pc_control", "read_screen"),
    ("find the word Session on screen", "screen", "pc_control", "find_text"),
    ("click the word Settings on screen", "screen", "pc_control", "click_text"),
    ("list the controls of Notepad", "screen", "pc_control", "ui_tree"),
    ("read the control searchBox", "screen", "pc_control", "ui_read"),
    # files
    ("find the invoice from March", "file", "file_control", "find"),
    ("delete the old draft.pdf", "file", "file_control", "delete"),
    ("copy the reports folder to my desktop", "file", "file_control", "copy"),
    ("move the draft to my documents", "file", "file_control", "move"),
    ("rename old.txt to new.txt", "file", "file_control", "rename"),
    ("zip these files", "file", "file_control", "zip"),
    ("unzip the archive", "file", "file_control", "unzip"),
    ("open the invoice from March.pdf", "file", "file_control", "open"),
    ("open the downloads folder", "file", "file_control", "open"),
    ("what's in my downloads folder", "file", "file_control", "list"),
    # clipboard
    ("read the clipboard", "clipboard", "clipboard_sentry", "read"),
    ("clear the clipboard", "clipboard", "clipboard_sentry", "clear"),
    ("copy this text to the clipboard", "clipboard", "clipboard_sentry", "write"),
    ("translate the clipboard to French", "clipboard", "clipboard_sentry", "translate"),
    ("summarize the clipboard", "clipboard", "clipboard_sentry", "analyze"),
    # documents
    ("create a word document for the meeting", "document", "documents", "create_docx"),
    ("make an excel spreadsheet", "document", "documents", "create_xlsx"),
    ("create a powerpoint deck", "document", "documents", "create_pptx"),
    ("merge these pdfs", "document", "documents", "pdf_merge"),
    # media / app / web / research keep their exact previous routes
    ("play daft punk", "media", "play_music", "play"),
    ("pause the music", "media", "play_music", "pause"),
    ("next song", "media", "play_music", "next"),
    ("turn the volume up", "media", "power_control", "volume_up"),
    ("open notepad", "app", "pc_control", "open_application"),
    ("close excel", "app", "pc_control", "close_application"),
    ("open youtube in the browser", "web", "browser_action", "browser_open"),
    ("go to https://example.com", "web", "browser_action", "browser_open"),
    ("research the best laptops of 2026", "research", "web_research", ""),
])
def test_each_deterministic_rule_routes_exactly(spoken, intent, tool, action):
    fast = decide.quick(spoken)

    assert fast is not None, f"{spoken!r} matched no rule"
    assert fast["intent"] == intent
    assert fast["tool"] == tool
    assert fast["action"] == action
    assert fast["source"] == "rule"
    # The same sentence must classify identically through the public path.
    assert decide.classify(spoken)[0] == intent


def test_minimize_all_windows_is_not_a_classifier_guess():
    """The exact live misroute: Laya said CODE, the rule says window control."""
    fast = decide.quick("minimize all windows")

    assert fast["rule"] == "window.minimize_all"
    assert fast["action"] == "minimize_all"


def test_priority_command_resolves_the_level_vocabulary():
    fast = decide.quick("set chrome priority to above normal")

    assert fast["args"]["name"] == "chrome"
    assert fast["args"]["level"] == "above_normal"


def test_a_volume_command_is_power_control_not_playback():
    fast = decide.quick("turn the volume up")

    assert fast["tool"] == "power_control"
    assert fast["args"]["action"] == "volume_up"


def test_a_site_name_resolves_to_its_real_url():
    fast = decide.quick("open youtube in the browser")

    assert fast["args"]["url"] == "https://www.youtube.com"


def test_an_explicit_url_beats_a_site_name():
    fast = decide.quick("open the dashboard at https://example.com")

    assert fast["args"]["url"] == "https://example.com"


# ── guards: conversation must not be dragged into automation ─────────────────

@pytest.mark.parametrize("spoken", [
    "find information about laptops",
    "tell me a joke",
    "what is the capital of France",
    "how are you today",
    "explain quantum computing",
])
def test_conversational_or_non_mechanical_requests_have_no_rule(spoken):
    assert decide.quick(spoken) is None, f"{spoken!r} must not match a rule"


@pytest.mark.parametrize("spoken", ["pause the music", "resume the music",
                                    "stop the music", "skip the song"])
def test_playback_controls_are_deterministic_media_rules(spoken):
    """Playback verbs are decided by the rule table, never by a model."""
    fast = decide.quick(spoken)

    assert fast is not None and fast["intent"] == "media"
    assert fast["tool"] == "play_music"


def test_finding_a_file_needs_a_file_noun():
    """'find X' is a file rule only when X names a real file/folder."""
    assert decide.quick("find the invoice from March")["tool"] == "file_control"
    assert decide.quick("find information about laptops") is None
    # ...and the vocabulary fallback still routes the research sentence.
    assert decide.classify("find information about laptops")[0] == "research"


def test_stopping_the_music_is_playback_not_a_process_kill():
    fast = decide.quick("stop the music")

    assert fast["intent"] == "media"
    assert fast["tool"] == "play_music"


def test_copying_text_to_the_clipboard_is_not_a_file_copy():
    fast = decide.quick("copy this text to the clipboard")

    assert fast["intent"] == "clipboard"
    assert fast["tool"] == "clipboard_sentry"


# ── rule hygiene ─────────────────────────────────────────────────────────────

def test_rule_names_are_unique_and_ordered():
    names = decide.rule_names()

    assert len(names) >= 40, "the rule table lost coverage"
    assert len(names) == len(set(names))


def test_every_rule_intent_is_declared():
    for rule in decide._RULES:
        assert rule.intent in decide.INTENTS, rule.name


def test_every_registry_rule_action_exists():
    """A rule pointing at an action the registry does not know is a dead end."""
    for rule in decide._RULES:
        if rule.tool in registry.TOOLS and rule.action:
            assert registry.get(rule.tool, rule.action) is not None, rule.name


# ── plan(): rules set the action, the chain still owns readiness ─────────────

def test_a_screenshot_goes_to_the_capture_controller_not_a_vision_model():
    result = decide.plan("take a screenshot", facts=_state())

    assert result["tool"] == "pc_control"
    assert result["controller"] == "pyautogui"
    assert result["args"]["action"] == "screen_screenshot"
    assert result["source"] == "rule"
    assert result["rule"] == "screen.screenshot"
    assert result["ready"] is True


def test_a_ui_tree_request_goes_to_ui_automation():
    result = decide.plan("list the controls of Notepad", facts=_state())

    assert result["tool"] == "pc_control"
    assert result["controller"] == "uia"
    assert result["args"]["action"] == "ui_tree"


def test_window_control_reports_the_master_switch_honestly():
    state = _state()
    state["switches"]["window_control"] = False

    result = decide.plan("minimize all windows", facts=state)

    assert result["ready"] is False
    assert "window control is switched off" in result["candidates"][0]["reason"]


def test_file_reads_work_with_the_switch_off_but_writes_do_not():
    state = _state()
    state["switches"]["file_control"] = False

    read = decide.plan("find the invoice from March", facts=state)
    write = decide.plan("delete the old draft.pdf", facts=state)

    assert read["ready"] is True, "listing/finding is read-only"
    assert write["ready"] is False
    assert "file control is switched off" in write["candidates"][0]["reason"]


def test_a_rule_never_rewrites_a_different_controller():
    """The screen chain prefers UI-TARS; a screenshot rule must override it."""
    result = decide.plan("take a screenshot", facts=_state(uitars=True))

    assert result["controller"] == "pyautogui"
    tools = [c["tool"] for c in result["candidates"]]
    assert tools[0] == "pc_control", "the deterministic action goes first"


# ── smart(): the fusion order ────────────────────────────────────────────────

def test_smart_prefers_a_rule_over_laya(monkeypatch):
    """Laya said CODE for this exact sentence live; the rule must still win."""
    from core import laya_decision

    monkeypatch.setattr(laya_decision, "last_classification",
                        lambda: {"category": "CODE", "confidence": 0.97,
                                 "request": "minimize all windows"})
    monkeypatch.setattr(laya_decision, "get_state", lambda: "ready")

    result = decide.smart("minimize all windows", facts=_state())

    assert result["source"] == "rule"
    assert result["rule"] == "window.minimize_all"
    assert result["tool"] == "window_control"


def test_smart_uses_layas_own_decision_when_no_rule_matches(monkeypatch):
    from core import laya_decision as real_laya
    from core import laya_decision

    monkeypatch.setattr(laya_decision, "last_classification",
                        lambda: {"category": "SEARCH", "confidence": 0.81,
                                 "request": "who won the race yesterday"})
    monkeypatch.setattr(laya_decision, "get_state",
                        lambda: real_laya.STATE_READY)
    monkeypatch.setattr(laya_decision, "classify_task",
                        lambda text, record=True: {"category": "SEARCH",
                                                   "confidence": 0.81,
                                                   "target": "web search"})

    result = decide.smart("who won the race yesterday", facts=_state())

    assert result["source"] == "laya"
    assert result["intent"] == "research"
    assert result["laya"]["category"] == "SEARCH"
    assert result["confidence"] == pytest.approx(0.81)


def test_smart_without_laya_falls_back_to_the_vocabulary(monkeypatch):
    from core import laya_decision

    monkeypatch.setattr(laya_decision, "last_classification", lambda: None)
    monkeypatch.setattr(laya_decision, "get_state",
                        lambda: laya_decision.STATE_MISSING_DEPENDENCY)

    result = decide.smart("tell me a joke", facts=_state())

    assert result["source"] == "classifier"
    assert result["intent"] == "chat"


def test_smart_never_blocks_on_a_loading_model(monkeypatch):
    from core import laya_decision

    called = []
    monkeypatch.setattr(laya_decision, "last_classification", lambda: None)
    monkeypatch.setattr(laya_decision, "get_state",
                        lambda: laya_decision.STATE_LOADING)
    monkeypatch.setattr(laya_decision, "classify_task",
                        lambda text, record=True: called.append(text))

    result = decide.smart("explain quantum computing to me", facts=_state())

    assert called == [], "a loading model must not be asked to decide in a turn"
    assert result["source"] == "classifier"


# ── render()/report() ────────────────────────────────────────────────────────

def test_render_names_the_source_the_tool_and_the_arguments():
    result = decide.plan("minimize all windows", facts=_state())
    text = decide.render(result)

    assert "CONTROL PLAN" in text
    assert "decided by: rule (rule window.minimize_all)" in text
    assert "window_control" in text
    assert '"action": "minimize_all"' in text


def test_report_quick_mode_uses_the_rule():
    text = decide.report("set chrome priority high", mode="quick")

    assert "system_control" in text
    assert "set_process_priority" in text


def test_report_for_a_conversation_sends_no_controller():
    text = decide.report("tell me a joke", mode="quick")

    assert "No controller is needed" in text


# ── main.py wiring ───────────────────────────────────────────────────────────

def test_smart_decide_is_declared_and_dispatched():
    assert '"name": "smart_decide"' in MAIN_SOURCE
    assert "_decide.smart(" in MAIN_SOURCE
    assert "_decide.render(" in MAIN_SOURCE
    assert '"SMART"' in MAIN_SOURCE
