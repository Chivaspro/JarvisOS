"""The cache sweep must never hold the window hostage.

Stage 2 of the optimizer walks the user's whole temp folder. That walk is the
only unbounded part, and the launcher used to wait up to 25 s for it before it
would even spawn main.py. These tests pin the split: the fast, app-owned cleanup
finishes before boot, the OS-temp walk continues in the still-alive launcher
process, and main.py is told not to run a second sweep.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import start_jarvis  # noqa: E402
from core import startup_optimizer as opt  # noqa: E402


_FAST_STATS = {
    "files": 2, "dirs": 1, "bytes": 2048, "skipped": 0,
    "timed_out": False, "db": True, "seconds": 0.01, "ran": True,
}


def test_fast_phase_completes_while_the_temp_sweep_is_still_running(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    seen_include_temp: list[bool] = []

    def fake_fast(base, started_at=None, time_budget=15.0, progress=None,
                  include_temp=True):
        seen_include_temp.append(include_temp)
        return dict(_FAST_STATS)

    def fake_temp(base, started_at=None, time_budget=15.0, progress=None,
                  stats=None):
        entered.set()
        release.wait(3.0)
        stats["files"] += 5

    monkeypatch.setattr(opt, "run_startup_optimization", fake_fast)
    monkeypatch.setattr(opt, "run_temp_sweep", fake_temp)

    handle = opt.start(tmp_path)

    assert handle.wait(1.0) is True, "the fast pass must release boot"
    assert entered.wait(1.0) is True, "the slow pass must keep running"
    assert seen_include_temp == [False]
    assert handle.summary().endswith(".")
    assert "continuing in the background" in handle.summary()

    release.set()
    assert handle.wait_all(2.0) is True
    assert handle.result["files"] == 7     # the slow pass updated the result


def test_fast_phase_does_not_touch_the_os_temp_folder(monkeypatch, tmp_path):
    calls: list[int] = []
    monkeypatch.setattr(opt, "_sweep_system_temp",
                        lambda *a, **k: calls.append(1))

    opt.run_startup_optimization(tmp_path, include_temp=False, progress=None)

    assert calls == []


def test_a_full_run_still_sweeps_the_os_temp_folder(monkeypatch, tmp_path):
    calls: list[int] = []
    monkeypatch.setattr(opt, "_sweep_system_temp",
                        lambda *a, **k: calls.append(1))

    opt.run_startup_optimization(tmp_path, progress=None)

    assert calls == [1]


def test_finish_optimizer_hands_over_even_when_the_sweep_is_not_done(monkeypatch):
    monkeypatch.delenv(start_jarvis.ENV_OPT_DONE, raising=False)
    monkeypatch.delenv(start_jarvis.ENV_OPT_SUMMARY, raising=False)
    waits: list[float] = []
    logs: list[str] = []

    class SlowHandle:
        def wait(self, timeout):
            waits.append(timeout)
            return False

        def summary(self):
            raise AssertionError("an unfinished sweep has no summary yet")

    try:
        start_jarvis._finish_optimizer(SlowHandle(), logs.append)
        assert os.environ[start_jarvis.ENV_OPT_DONE] == "1"
        assert waits and waits[0] <= 3.5, "boot must not wait 25 s for temp files"
        assert any("background" in line.lower() for line in logs)
    finally:
        os.environ.pop(start_jarvis.ENV_OPT_DONE, None)
        os.environ.pop(start_jarvis.ENV_OPT_SUMMARY, None)


def test_the_handoff_flag_is_set_before_main_is_spawned():
    source = (ROOT / "start_jarvis.py").read_text(encoding="utf-8")
    finish = source.index("_finish_optimizer(optimizer, log)")
    spawn = source.index("return launch(chosen.exe")
    assert finish < spawn, (
        "JARVIS_STARTUP_OPT_DONE must be in the environment the child inherits, "
        "or main.py runs the whole sweep a second time")


def test_a_background_pass_that_dies_never_blocks_boot(monkeypatch, tmp_path):
    monkeypatch.setattr(opt, "run_startup_optimization",
                        lambda *a, **k: dict(_FAST_STATS))
    monkeypatch.setattr(opt, "run_temp_sweep",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    handle = opt.start(tmp_path)

    assert handle.wait(1.0) is True
    assert handle.wait_all(2.0) is True
    assert handle.result["ran"] is True
