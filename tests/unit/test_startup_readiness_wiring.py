"""The startup surfaces may only say what is true.

These are structural tests on purpose: the bug was not a wrong value in one
function, it was three places independently deciding that JARVIS was ready.
Each assertion names the surface that used to lie.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UI = ROOT / "ui.py"
MAIN = ROOT / "main.py"
LAUNCHER = ROOT / "start_jarvis.py"
LAUNCH_UI = ROOT / "launch_ui.py"


def _class_function(path: Path, class_name: str, function_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == class_name)
    return next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == function_name)


def test_the_hud_default_is_booting_not_a_stuck_initialising():
    init = _class_function(UI, "HudCanvas", "__init__")
    assigned = []
    for node in ast.walk(init):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "state":
                assigned.append(ast.literal_eval(node.value))
    assert assigned == ["BOOTING"], (
        "HudCanvas must start in BOOTING; the old default INITIALISING only "
        "changed when the Live session connected, so a failed connect left it "
        "on screen forever"
    )


def test_saving_a_key_does_not_claim_listening():
    done = _class_function(UI, "MainWindow", "_on_setup_done")
    constants = {n.value for n in ast.walk(done)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "CONNECTING" in constants
    assert "LISTENING" not in constants, (
        "the setup overlay saved a key and immediately applied LISTENING, "
        "before the backend process had even connected"
    )


def test_ready_is_set_only_right_after_listening():
    lines = MAIN.read_text(encoding="utf-8").splitlines()
    listening = [i for i, line in enumerate(lines, 1)
                 if 'self.ui.set_state("LISTENING")' in line]
    ready = [i for i, line in enumerate(lines, 1)
             if "_startup_state.READY" in line]
    assert listening and ready
    ready_line = min(ready)
    before = [i for i in listening if i < ready_line]
    assert before, "READY must follow a real LISTENING transition"
    assert ready_line - max(before) <= 6, (
        "READY drifted away from the actual LISTENING transition")


def test_every_startup_marker_is_written_to_the_audit_log():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    markup = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_markup")
    calls = [n for n in ast.walk(markup)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "event"]
    assert calls, "_markup must also record the marker in startup.log"
    source = MAIN.read_text(encoding="utf-8")
    assert 'startup.log' in source
    assert "_startup_state.configure(" in source


def test_the_launcher_never_claims_online_before_the_core_is_ready():
    for path in (LAUNCHER, LAUNCH_UI):
        text = path.read_text(encoding="utf-8")
        assert "J.A.R.V.I.S ONLINE" not in text, (
            f"{path.name} still announces ONLINE while main.py is only being "
            "spawned"
        )


def test_degraded_and_reconnect_have_clear_human_labels():
    source = UI.read_text(encoding="utf-8")
    assert '"DEGRADED": "CONNECTION ISSUE"' in source
    assert '"RECONNECTING"' in source
