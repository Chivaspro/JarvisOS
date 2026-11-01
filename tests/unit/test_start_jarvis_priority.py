"""The launcher hands the CPU profile to the child before it starts.

Two things have to happen in the right order:

1. the thread budget must be in the environment passed to ``Popen`` — the
   numerical libraries read ``OMP_NUM_THREADS`` while they import, so patching
   the child after it starts would be too late;
2. the priority class is raised on the live child (it needs a PID, so it can
   only happen after ``Popen``).

An explicit user environment variable always wins, and a refused priority call
must never block the launch.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import start_jarvis as sj  # noqa: E402


class _Proc:
    pid = 4321

    def wait(self):
        return 0


def _fake_profile(priority="high", cap=3):
    calls = {"priority": [], "env_profile": []}

    def _apply(level, pid=None):
        calls["priority"].append((level, pid))
        return (True, f"priority set to {level}")

    return types.SimpleNamespace(
        PRIORITY_CHOICES=("idle", "below_normal", "normal", "above_normal", "high"),
        read_profile=lambda: {"cpu_priority": priority, "thread_cap": cap},
        thread_environment=lambda profile: {
            "OMP_NUM_THREADS": str(profile["thread_cap"] or 2),
            "TOKENIZERS_PARALLELISM": "false",
        },
        apply_priority=_apply,
    ), calls


def _capture_popen(monkeypatch, priority="high", cap=3):
    captured: dict = {}
    perf, calls = _fake_profile(priority, cap)

    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setattr(sj, "_performance_profile", lambda: perf)
    monkeypatch.setattr(sj, "subprocess", types.SimpleNamespace(
        Popen=_popen, CREATE_NO_WINDOW=getattr(sj.subprocess, "CREATE_NO_WINDOW", 0)))
    monkeypatch.setattr(sj, "_has_console", lambda: True)
    return captured, calls


def test_the_loader_finds_the_shared_profile_module():
    perf = sj._performance_profile()
    assert perf is not None
    assert perf.read_profile()["cpu_priority"] in perf.PRIORITY_CHOICES


def test_launch_sets_the_thread_budget_in_the_child_environment(monkeypatch):
    captured, _calls = _capture_popen(monkeypatch)

    assert sj.launch("python.exe") == 0

    assert captured["env"]["OMP_NUM_THREADS"] == "3"
    assert captured["env"]["TOKENIZERS_PARALLELISM"] == "false"
    assert captured["env"][sj.ENV_BOOTSTRAPPED] == "1"


def test_launch_raises_the_priority_on_the_child_pid(monkeypatch):
    _captured, calls = _capture_popen(monkeypatch, priority="above_normal")

    sj.launch("python.exe")

    assert calls["priority"] == [("above_normal", _Proc.pid)]


def test_an_explicit_user_thread_setting_is_never_overwritten(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "16")
    captured, _calls = _capture_popen(monkeypatch)

    sj.launch("python.exe")

    assert captured["env"]["OMP_NUM_THREADS"] == "16"


def test_a_refused_priority_does_not_block_the_launch(monkeypatch):
    captured: dict = {}

    def _popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _Proc()

    perf = types.SimpleNamespace(
        PRIORITY_CHOICES=("idle", "below_normal", "normal", "above_normal", "high"),
        read_profile=lambda: {"cpu_priority": "high", "thread_cap": 3},
        thread_environment=lambda profile: {"OMP_NUM_THREADS": "3"},
        apply_priority=lambda level, pid=None: (False, "access denied"),
    )
    monkeypatch.setattr(sj, "_performance_profile", lambda: perf)
    monkeypatch.setattr(sj, "subprocess", types.SimpleNamespace(
        Popen=_popen, CREATE_NO_WINDOW=getattr(sj.subprocess, "CREATE_NO_WINDOW", 0)))
    monkeypatch.setattr(sj, "_has_console", lambda: True)

    lines: list[str] = []
    assert sj.launch("python.exe", log=lines.append) == 0
    assert captured["env"]["OMP_NUM_THREADS"] == "3"
    assert any("access denied" in line for line in lines)


def test_no_profile_file_means_a_plain_launch(monkeypatch):
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    captured: dict = {}

    def _popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setattr(sj, "_performance_profile", lambda: None)
    monkeypatch.setattr(sj, "subprocess", types.SimpleNamespace(
        Popen=_popen, CREATE_NO_WINDOW=getattr(sj.subprocess, "CREATE_NO_WINDOW", 0)))
    monkeypatch.setattr(sj, "_has_console", lambda: True)

    assert sj.launch("python.exe") == 0
    assert "OMP_NUM_THREADS" not in captured["env"]


def test_the_on_started_callback_still_runs_after_the_priority_change(monkeypatch):
    _captured, _calls = _capture_popen(monkeypatch)
    fired: list[bool] = []

    sj.launch("python.exe", on_started=lambda: fired.append(True))

    assert fired == [True]
