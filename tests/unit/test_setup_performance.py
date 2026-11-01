"""Setup ▸ Optimize: the full pass, pinned without touching the real machine.

Optimize now runs: housekeeping → precompile → CPU profile (applied to a live
instance) → the reversible Windows performance suite (graphics/effects/power,
GPU preference) → a read-only state summary. Every Windows-facing call is
monkeypatched here, so these tests never change a registry value or a power
plan; what they pin is the ORDER, the confirmation gate, the backup/restore of
the graphics preference, and the honest reporting of each outcome.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setup_jarvis  # noqa: E402


class _FakePerf:
    """The slice of core.performance_profile the wizard calls."""

    def __init__(self, profile=None):
        self.profile = dict(profile or {"cpu_priority": "above_normal",
                                        "thread_cap": 0})
        self.saved = []
        self.power = []
        self.priority_calls = []

    def read_profile(self):
        return dict(self.profile)

    def write_profile(self, profile):
        self.profile = dict(profile)
        self.saved.append(dict(profile))
        return True

    def describe(self, profile=None):
        p = profile or self.profile
        return f"CPU priority {p['cpu_priority']} · numerical threads {p['thread_cap'] or 2}"

    def default_thread_cap(self):
        return 2

    def apply_priority(self, level, pid=None):
        self.priority_calls.append((level, pid))
        return (True, f"priority set to {level}")

    def set_power_plan(self, name="high"):
        self.power.append(name)
        return (True, f"power plan set to {name}")


# ── which sources are in scope ──────────────────────────────────────────────

def test_precompile_skips_vendored_user_and_test_trees(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "keep.py").write_text("x = 1\n", encoding="utf-8")
    for skipped in (".venv/Lib/site-packages", "data", "tests", ".cache",
                    "node_modules/pkg", "models/m"):
        (tmp_path / skipped).mkdir(parents=True)
        (tmp_path / skipped / "skip.py").write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)

    sources = setup_jarvis._python_sources_in_scope()
    assert [p.name for p in sources] == ["keep.py"]


def test_compile_project_reports_a_broken_module_instead_of_crashing(
        tmp_path, monkeypatch, capsys):
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)

    report = setup_jarvis.compile_project()

    assert report["sources"] == 2
    assert report["ok"] is False
    assert report["failures"] == 1
    assert report["seconds"] >= 0
    assert "broken.py" in capsys.readouterr().out


def test_compile_project_refreshes_bytecode_for_the_next_import(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)

    first = setup_jarvis.compile_project()
    second = setup_jarvis.compile_project()

    assert first["ok"] is True and first["updated"] == 1
    assert second["ok"] is True and second["updated"] == 0, \
        "an unchanged module must not be recompiled"
    assert list((tmp_path / "pkg" / "__pycache__").glob("mod.*.pyc"))


# ── the live-instance priority path ─────────────────────────────────────────

def test_no_lock_means_no_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    applied, detail = setup_jarvis.apply_priority_to_running()
    assert applied is None
    assert "not running" in detail


def test_a_live_instance_gets_the_saved_priority(monkeypatch):
    perf = _FakePerf({"cpu_priority": "high", "thread_cap": 2})
    monkeypatch.setattr(setup_jarvis, "_performance_profile", lambda: perf)
    monkeypatch.setattr(setup_jarvis, "_lock_pid", lambda: 4242)
    monkeypatch.setattr(setup_jarvis, "_pid_alive", lambda pid: pid == 4242)

    applied, detail = setup_jarvis.apply_priority_to_running()

    assert applied is True
    assert perf.priority_calls == [("high", 4242)]
    assert "4242" in detail


# ── the interactive profile editor ─────────────────────────────────────────

def test_configure_performance_saves_the_choices(monkeypatch, capsys):
    perf = _FakePerf()
    answers = iter(["5", "3"])
    monkeypatch.setattr(setup_jarvis, "_performance_profile", lambda: perf)
    monkeypatch.setattr(setup_jarvis, "ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: False)

    assert setup_jarvis.configure_performance() is True
    assert perf.saved[-1] == {"cpu_priority": "high", "thread_cap": 3}
    assert perf.power == []
    assert "Saved" in capsys.readouterr().out


def test_configure_performance_can_set_the_power_plan(monkeypatch):
    perf = _FakePerf()
    answers = iter(["4", "0"])
    monkeypatch.setattr(setup_jarvis, "_performance_profile", lambda: perf)
    monkeypatch.setattr(setup_jarvis, "ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: True)

    setup_jarvis.configure_performance()

    assert perf.power == ["high"]


def test_configure_performance_keeps_the_cap_on_a_typo(monkeypatch):
    perf = _FakePerf({"cpu_priority": "normal", "thread_cap": 4})
    answers = iter(["3", "lots"])
    monkeypatch.setattr(setup_jarvis, "_performance_profile", lambda: perf)
    monkeypatch.setattr(setup_jarvis, "ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: False)

    setup_jarvis.configure_performance()

    assert perf.saved[-1]["thread_cap"] == 4


# ── the Optimize orchestration ──────────────────────────────────────────────

def _hermetic_windows_steps(monkeypatch, order: list):
    """Stand in for the real Windows calls: record, never touch the machine."""
    monkeypatch.setattr(setup_jarvis, "apply_windows_performance", lambda confirm=True: (
        order.append("windows") or (False, "skipped")))
    monkeypatch.setattr(setup_jarvis, "_print_windows_tuning",
                        lambda: order.append("state"))


def test_optimize_runs_housekeeping_then_precompiles_then_applies_the_profile(
        monkeypatch, capsys):
    order = []
    monkeypatch.setattr(setup_jarvis, "compile_project", lambda: (
        order.append("compile") or {"ok": True, "sources": 10, "updated": 2,
                                    "failures": 0, "seconds": 0.4}))
    monkeypatch.setattr(setup_jarvis, "_performance_profile", lambda: _FakePerf())
    monkeypatch.setattr(setup_jarvis, "apply_priority_to_running", lambda: (
        order.append("priority") or (None, "J.A.R.V.I.S. is not running")))
    _hermetic_windows_steps(monkeypatch, order)

    from core import maintenance
    monkeypatch.setattr(maintenance, "run_maintenance", lambda: (
        order.append("clean") or {"logs": {"removed": 0, "bytes": 0, "kept": 1},
                                  "cache": {"removed": 0, "bytes": 0},
                                  "pycache": {"removed": 0, "bytes": 0},
                                  "reclaimed_bytes": 0, "reclaimed_mb": 0.0}))
    monkeypatch.setattr(maintenance, "format_report", lambda r: "MAINTENANCE")

    report = setup_jarvis.optimize()

    assert order == ["clean", "compile", "priority", "windows", "state"]
    assert report["compile"]["sources"] == 10
    assert report["performance"]["applied"] is None
    assert report["windows"] == {"applied": False, "detail": "skipped"}
    out = capsys.readouterr().out
    assert "10 source module(s) verified" in out
    assert "applies at the next launch" in out
    assert "Step 4/5" in out and "Step 5/5" in out


def test_optimize_survives_a_failing_maintenance_step(monkeypatch, capsys):
    from core import maintenance

    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(maintenance, "run_maintenance", explode)
    monkeypatch.setattr(setup_jarvis, "compile_project", lambda: {
        "ok": True, "sources": 1, "updated": 0, "failures": 0, "seconds": 0.1})
    monkeypatch.setattr(setup_jarvis, "_performance_profile", lambda: _FakePerf())
    monkeypatch.setattr(setup_jarvis, "apply_priority_to_running",
                        lambda: (None, "J.A.R.V.I.S. is not running"))
    _hermetic_windows_steps(monkeypatch, [])

    report = setup_jarvis.optimize()

    assert "compile" in report
    assert "boom" in capsys.readouterr().out


def test_optimize_reports_a_failing_windows_step_honestly(monkeypatch, capsys):
    from core import maintenance
    monkeypatch.setattr(maintenance, "run_maintenance",
                        lambda: {"reclaimed_mb": 0.0})
    monkeypatch.setattr(maintenance, "format_report", lambda r: "MAINTENANCE")
    monkeypatch.setattr(setup_jarvis, "compile_project", lambda: {
        "ok": True, "sources": 1, "updated": 0, "failures": 0, "seconds": 0.1})
    monkeypatch.setattr(setup_jarvis, "_performance_profile", lambda: _FakePerf())
    monkeypatch.setattr(setup_jarvis, "apply_priority_to_running",
                        lambda: (None, "J.A.R.V.I.S. is not running"))
    monkeypatch.setattr(setup_jarvis, "apply_windows_performance",
                        lambda confirm=True: (False, "powercfg refused"))
    monkeypatch.setattr(setup_jarvis, "_print_windows_tuning", lambda: None)

    report = setup_jarvis.optimize()

    assert report["windows"] == {"applied": False, "detail": "powercfg refused"}
    assert "powercfg refused" in capsys.readouterr().out


# ── the reversible Windows suite (fake backend, no real registry) ───────────

class _FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    REG_SZ = 1
    REG_DWORD = 4


class _FakeJoptimizer:
    """The slice of core.joptimizer the wizard calls, with a memory registry."""

    IS_WINDOWS = True
    winreg = _FakeWinreg()
    REGISTRY_TARGETS = {
        "visual_effects": ("HKCU", r"Software\VisualEffects", ["VisualFXSetting"]),
        "transparency": ("HKCU", r"Software\Personalize", ["EnableTransparency"]),
        "game_dvr": ("HKCU", r"System\GameConfigStore", ["GameDVR_Enabled"]),
        "game_mode": ("HKCU", r"Software\GameBar", ["AutoGameModeEnabled"]),
    }

    def __init__(self):
        self.registry: dict = {}
        self.suite_calls = 0
        self.deleted: list = []
        self.snapshots: list = []
        self.reverted = 0

    def registry_read(self, root, path, name):
        key = (path, name)
        if key in self.registry:
            return {"exists": True, "value": self.registry[key], "kind": 1}
        return {"exists": False}

    def registry_write(self, root, path, name, value, kind):
        self.registry[(path, name)] = value

    def registry_delete_value(self, root, path, name):
        self.deleted.append((path, name))
        self.registry.pop((path, name), None)

    def jarvis_ultimate_optimize(self, auto=False, progress=None):
        self.suite_calls += 1
        return "✓ Power plan: high\n✓ Visual effects: performance"

    def list_snapshots(self):
        return list(self.snapshots)

    def jarvis_revert_latest(self):
        self.reverted += 1
        return "Reverted to snapshot: snap.json"

    def get_active_power_plan(self):
        return "Power Scheme GUID: 8c5e7fda  (High performance)"

    def get_system_summary(self):
        return {"cpu": 12.0, "ram": 41.0, "ram_gb": 16.0,
                "disk_free_gb": 120.0, "disk_total_gb": 500.0,
                "uptime": "1:00:00", "os": "Windows"}

    def health_score(self, summary=None):
        return 92

    def top_processes(self, limit=6):
        return ["python (PID 1) · CPU 1.0% · RAM 1.0%"]


def _fake_backend(monkeypatch, backend: _FakeJoptimizer) -> None:
    monkeypatch.setattr(setup_jarvis, "_joptimizer", lambda: backend)
    monkeypatch.setattr(setup_jarvis, "_gpu_targets",
                        lambda: [Path(r"C:\jarvis\.venv\Scripts\python.exe"),
                                 Path(r"C:\jarvis\.venv\Scripts\pythonw.exe")])


def test_the_windows_suite_applies_through_the_backend(monkeypatch, tmp_path, capsys):
    backend = _FakeJoptimizer()
    _fake_backend(monkeypatch, backend)
    monkeypatch.setattr(setup_jarvis, "_tuning_file",
                        lambda: tmp_path / "windows_tuning.json")
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: True)

    applied, detail = setup_jarvis.apply_windows_performance(confirm=True)

    assert applied is True and detail == "applied"
    assert backend.suite_calls == 1
    # The graphics preference was written for both Python executables…
    assert sum(1 for (path, _n) in backend.registry
               if path == setup_jarvis._GPU_PREF_PATH) == 2
    assert "high performance" in capsys.readouterr().out


def test_the_windows_suite_is_never_applied_without_a_yes(monkeypatch, capsys):
    backend = _FakeJoptimizer()
    _fake_backend(monkeypatch, backend)
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: False)

    applied, detail = setup_jarvis.apply_windows_performance(confirm=True)

    assert applied is False and detail == "skipped"
    assert backend.suite_calls == 0
    assert backend.registry == {}, "nothing may change on a declined prompt"
    assert "Skipped" in capsys.readouterr().out


def test_the_windows_suite_is_honest_off_windows(monkeypatch):
    class _Posix(_FakeJoptimizer):
        IS_WINDOWS = False

    _fake_backend(monkeypatch, _Posix())

    applied, detail = setup_jarvis.apply_windows_performance(confirm=False)

    assert applied is False and detail == "Windows-only"


def test_the_non_interactive_default_cannot_apply_the_suite(monkeypatch):
    """_confirm returns its default when stdin is not a TTY; that default is NO."""
    assert "default=False" in (
        ROOT / "setup_jarvis.py").read_text(encoding="utf-8").split(
            "def apply_windows_performance")[1].split("def configure_windows")[0]


def test_gpu_preference_backs_up_and_restores_the_previous_value(monkeypatch, tmp_path):
    backend = _FakeJoptimizer()
    _fake_backend(monkeypatch, backend)
    monkeypatch.setattr(setup_jarvis, "_tuning_file",
                        lambda: tmp_path / "windows_tuning.json")

    exe = r"C:\jarvis\.venv\Scripts\python.exe"
    backend.registry[(setup_jarvis._GPU_PREF_PATH, exe)] = "GpuPreference=0;"

    good, _ = setup_jarvis.apply_gpu_preference(high=True)
    assert good is True
    assert backend.registry[(setup_jarvis._GPU_PREF_PATH, exe)] == "GpuPreference=2;"

    good, detail = setup_jarvis.restore_gpu_preference()
    assert good is True and "restored" in detail
    assert backend.registry[(setup_jarvis._GPU_PREF_PATH, exe)] == "GpuPreference=0;"


def test_gpu_preference_removes_entries_that_did_not_exist_before(monkeypatch, tmp_path):
    backend = _FakeJoptimizer()
    _fake_backend(monkeypatch, backend)
    monkeypatch.setattr(setup_jarvis, "_tuning_file",
                        lambda: tmp_path / "windows_tuning.json")

    setup_jarvis.apply_gpu_preference(high=True)
    setup_jarvis.restore_gpu_preference()

    assert backend.deleted, "an entry we created must be deleted on restore"
    assert not any(path == setup_jarvis._GPU_PREF_PATH
                   for (path, _n) in backend.registry)


def test_undo_restores_the_snapshot_and_the_gpu_preference(monkeypatch, tmp_path, capsys):
    backend = _FakeJoptimizer()
    backend.snapshots = [Path("snap.json")]
    _fake_backend(monkeypatch, backend)
    monkeypatch.setattr(setup_jarvis, "_tuning_file",
                        lambda: tmp_path / "windows_tuning.json")
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: True)
    setup_jarvis.apply_gpu_preference(high=True)

    assert setup_jarvis.undo_windows_performance() is True

    assert backend.reverted == 1
    out = capsys.readouterr().out
    assert "snap.json" in out
    assert "restored" in out


def test_undo_with_nothing_to_undo_says_so(monkeypatch, tmp_path, capsys):
    backend = _FakeJoptimizer()
    _fake_backend(monkeypatch, backend)
    monkeypatch.setattr(setup_jarvis, "_tuning_file",
                        lambda: tmp_path / "windows_tuning.json")

    assert setup_jarvis.undo_windows_performance() is False
    assert "Nothing to undo" in capsys.readouterr().out


def test_undo_is_never_applied_without_a_yes(monkeypatch, tmp_path, capsys):
    """A bare pytest snapshot in the real folder was once restored by a probe —
    the gate's non-interactive default must stay NO."""
    backend = _FakeJoptimizer()
    backend.snapshots = [Path("snap.json")]
    _fake_backend(monkeypatch, backend)
    monkeypatch.setattr(setup_jarvis, "_tuning_file",
                        lambda: tmp_path / "windows_tuning.json")
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: False)
    setup_jarvis.apply_gpu_preference(high=True)

    assert setup_jarvis.undo_windows_performance() is False
    assert backend.reverted == 0, "a declined prompt must not restore"
    assert "Skipped" in capsys.readouterr().out
    # …and the graphics preference is still what the suite wrote.
    assert all(value == "GpuPreference=2;" for (path, _n), value
               in backend.registry.items() if path == setup_jarvis._GPU_PREF_PATH)


def test_windows_tuning_rows_are_read_from_the_real_values(monkeypatch):
    backend = _FakeJoptimizer()
    _fake_backend(monkeypatch, backend)
    backend.registry[(r"Software\VisualEffects", "VisualFXSetting")] = 2
    backend.registry[(r"Software\Personalize", "EnableTransparency")] = 0
    backend.registry[(r"System\GameConfigStore", "GameDVR_Enabled")] = 0
    backend.registry[(r"Software\GameBar", "AutoGameModeEnabled")] = 1

    rows = dict((label, (value, state))
                for label, value, state in setup_jarvis._windows_tuning_rows())

    assert rows["Visual effects"][0] == "best performance"
    assert rows["Visual effects"][1] == "ok"
    assert rows["UI transparency"][0] == "off"
    assert rows["Game DVR capture"][0] == "off"
    assert rows["Game Mode"][0] == "on"
    assert rows["Power plan"][1] == "ok", "High performance is the good state"


def test_windows_tuning_rows_are_honest_off_windows(monkeypatch):
    class _Posix(_FakeJoptimizer):
        IS_WINDOWS = False

    _fake_backend(monkeypatch, _Posix())

    rows = setup_jarvis._windows_tuning_rows()

    assert rows == [("Windows tuning", "Windows-only", "opt")]


# ── menu wiring ────────────────────────────────────────────────────────────

def test_optimize_and_the_profile_editor_are_reachable_from_clean_and_maintain():
    assert setup_jarvis._lookup_choice("8", "maintain-menu")[1] is setup_jarvis.optimize
    assert (setup_jarvis._lookup_choice("p", "maintain-menu")[1]
            is setup_jarvis.configure_performance)


def test_the_windows_rows_are_reachable_from_clean_and_maintain():
    assert (setup_jarvis._lookup_choice("w", "maintain-menu")[1]
            is setup_jarvis.configure_windows_performance)
    assert (setup_jarvis._lookup_choice("u", "maintain-menu")[1]
            is setup_jarvis.undo_windows_performance)
    assert (setup_jarvis._lookup_choice("j", "maintain-menu")[1]
            is setup_jarvis.windows_performance_report)


def test_the_optimize_row_says_what_the_full_pass_does():
    row = next(r for r in setup_jarvis.SUBMENUS["maintain-menu"] if r[0] == "8")
    description = row[2].lower()
    for word in ("cpu", "windows", "graphics"):
        assert word in description, f"the Optimize row does not mention {word}"


def test_the_saved_profile_is_written_to_the_real_config_schema(tmp_path):
    """The key lives under "performance" so config.settings readers ignore it."""
    from core import performance_profile

    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"features": {"laya_ai": True}}), encoding="utf-8")
    assert performance_profile.write_profile(
        {"cpu_priority": "high", "thread_cap": 4}, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["features"] == {"laya_ai": True}
    assert data["performance"] == {"cpu_priority": "high", "thread_cap": 4}
