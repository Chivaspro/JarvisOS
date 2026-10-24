"""A double-click must notice missing packages/keys and offer to fix them.

Both launchers (run_jarvis.bat and run_jarvis.vbs) now go through
``setup_jarvis.py --launch``. These tests pin the gate's decisions: prompt only
when something is actually missing, never block a launch, and use a real dialog
on the windowless path where a console prompt would silently answer "no".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setup_jarvis as sj  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    """Record every repair side effect; none of them may actually run."""
    seen: list[str] = []
    monkeypatch.setattr(sj, "install_python_deps",
                        lambda: seen.append("install") or True)
    monkeypatch.setattr(sj, "configure_keys",
                        lambda *a, **k: seen.append("keys") or True)
    monkeypatch.setattr(sj, "_ensure_env", lambda: seen.append("env"))
    monkeypatch.setattr(sj, "_open_key_setup",
                        lambda: seen.append("key_window") or True)
    monkeypatch.setattr(sj, "_gui_message", lambda *a, **k: None)
    monkeypatch.setattr(sj, "warn", lambda *a, **k: None)
    monkeypatch.setattr(sj, "info", lambda *a, **k: None)
    return seen


def test_a_complete_install_asks_nothing(monkeypatch, calls):
    asked: list = []
    monkeypatch.setattr(sj, "_launch_runtime_complete", lambda: True)
    monkeypatch.setattr(sj, "_gemini_key", lambda: "key-present")
    monkeypatch.setattr(sj, "_launch_confirm",
                        lambda *a, **k: asked.append(a) or True)

    sj._ensure_launch_ready()

    assert calls == []
    assert asked == []


def test_missing_packages_offer_and_install(monkeypatch, calls):
    monkeypatch.setattr(sj, "_launch_runtime_complete", lambda: False)
    monkeypatch.setattr(sj, "_gemini_key", lambda: "key-present")
    monkeypatch.setattr(sj, "_launch_confirm", lambda *a, **k: True)

    sj._ensure_launch_ready()

    assert calls == ["install"]


def test_declining_an_install_still_allows_the_launch(monkeypatch, calls):
    monkeypatch.setattr(sj, "_launch_runtime_complete", lambda: False)
    monkeypatch.setattr(sj, "_gemini_key", lambda: "key-present")
    monkeypatch.setattr(sj, "_launch_confirm", lambda *a, **k: False)

    sj._ensure_launch_ready()

    assert calls == []


def test_missing_key_opens_the_console_wizard(monkeypatch, calls):
    monkeypatch.setattr(sj, "_launch_runtime_complete", lambda: True)
    monkeypatch.setattr(sj, "_gemini_key", lambda: "")
    monkeypatch.setattr(sj, "_no_console", lambda: False)
    monkeypatch.setattr(sj, "_launch_confirm", lambda *a, **k: True)

    sj._ensure_launch_ready()

    assert calls == ["keys", "env"]


def test_windowless_launch_uses_a_real_console_for_keys(monkeypatch, calls):
    monkeypatch.setattr(sj, "_launch_runtime_complete", lambda: True)
    monkeypatch.setattr(sj, "_gemini_key", lambda: "")
    monkeypatch.setattr(sj, "_no_console", lambda: True)
    monkeypatch.setattr(sj, "_launch_confirm", lambda *a, **k: True)

    sj._ensure_launch_ready()

    assert calls == ["key_window"], (
        "configure_keys() would call input() with no stdin under pythonw; the "
        "wizard has to run in its own console")


def test_a_failed_probe_never_blocks_the_launch(monkeypatch, calls):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(sj, "_launch_runtime_complete", boom)
    monkeypatch.setattr(sj, "_gemini_key", lambda: "key-present")
    monkeypatch.setattr(sj, "_launch_confirm", lambda *a, **k: True)

    sj._ensure_launch_ready()          # must not raise

    assert calls == []


def test_launch_runs_the_gate_before_the_core(monkeypatch, tmp_path):
    entry = tmp_path / "start_jarvis.py"
    entry.write_text("", encoding="utf-8")
    order: list[str] = []
    spawned: list[list[str]] = []

    class FakeProc:
        pass

    monkeypatch.setattr(sj, "START", entry)
    monkeypatch.setattr(sj, "_IS_WIN", True)
    monkeypatch.setattr(sj, "_no_console", lambda: False)
    monkeypatch.setattr(sj, "_ensure_launch_ready",
                        lambda: order.append("gate"))
    monkeypatch.setattr(sj, "auto_fix_gemini_before_launch",
                        lambda: order.append("fix"))
    monkeypatch.setattr(sj.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd) or FakeProc())
    monkeypatch.setattr(sj, "info", lambda *a, **k: None)
    monkeypatch.setattr(sj, "ok", lambda *a, **k: None)

    assert sj.launch() == 0
    assert order == ["gate", "fix"]
    assert spawned and spawned[0][-1].endswith("start_jarvis.py")


def test_windowless_launch_keeps_the_launcher_splash(monkeypatch, tmp_path):
    entry = tmp_path / "start_jarvis.py"
    entry.write_text("", encoding="utf-8")
    spawned: list[list[str]] = []

    class FakeProc:
        pass

    monkeypatch.setattr(sj, "START", entry)
    monkeypatch.setattr(sj, "_IS_WIN", True)
    monkeypatch.setattr(sj, "_no_console", lambda: True)
    monkeypatch.setattr(sj, "_ensure_launch_ready", lambda: None)
    monkeypatch.setattr(sj, "auto_fix_gemini_before_launch", lambda: None)
    monkeypatch.setattr(sj.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd) or FakeProc())
    monkeypatch.setattr(sj, "info", lambda *a, **k: None)
    monkeypatch.setattr(sj, "ok", lambda *a, **k: None)

    sj.launch()

    assert "--console-ui" in spawned[0]


def test_pythonw_can_import_the_wizard():
    source = Path(sj.__file__).read_text(encoding="utf-8")
    assert "_COLOR = bool(sys.stdout and sys.stdout.isatty())" in source, (
        "pythonw sets sys.stdout to None; the old isatty() call crashed the "
        "wizard before it could show the prompt")


def test_both_launchers_route_through_the_gate():
    bat = (ROOT / "run_jarvis.bat").read_text(encoding="utf-8")
    vbs = (ROOT / "run_jarvis.vbs").read_text(encoding="utf-8")
    assert 'setup_jarvis.py" --launch' in bat
    assert "setup_jarvis.py" in vbs and "--launch" in vbs
