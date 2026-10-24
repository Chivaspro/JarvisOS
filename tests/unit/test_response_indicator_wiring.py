"""The end-of-turn signal is distinct from a tool finishing.

``clear_chat_activity()`` is called after EVERY tool response, because the tool
rows are per-step. It therefore cannot also mean "the model turn is over" —
that is what made the working indicator ambiguous. The real turn ends at
``server_content.turn_complete`` and must call the dedicated hook, while the
tool path keeps its per-step clear.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

UI = ROOT / "ui.py"
MAIN = ROOT / "main.py"


def _calls_in(node, name: str) -> list[ast.Call]:
    out = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        fn = call.func
        if isinstance(fn, ast.Attribute) and fn.attr == name:
            out.append(call)
    return out


def test_turn_complete_uses_the_dedicated_turn_finished_hook():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    blocks = [n for n in ast.walk(tree)
              if isinstance(n, ast.If)
              and isinstance(n.test, ast.Attribute)
              and n.test.attr == "turn_complete"]
    assert blocks, "the server turn_complete branch must still exist"
    block = blocks[0]
    assert _calls_in(block, "turn_finished"), (
        "turn_complete must end the turn through ui.turn_finished()")
    assert not _calls_in(block, "clear_chat_activity"), (
        "clear_chat_activity() is the per-tool clear; it cannot mark turn end")


def test_the_tool_path_keeps_the_per_step_clear():
    text = MAIN.read_text(encoding="utf-8")
    assert '        try:\n            self.ui.clear_chat_activity()' in text, (
        "a tool response still clears its own activity row")


def test_the_proxy_forwards_turn_finished():
    tree = ast.parse(UI.read_text(encoding="utf-8"))
    proxies = [n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "JarvisUI"]
    assert proxies, "the thread-safe UI proxy must exist"
    names = {n.name for n in proxies[0].body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "turn_finished" in names, (
        "backend threads must be able to end the turn through the proxy")
