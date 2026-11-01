"""The CPU/thread profile shared by the wizard and the launcher.

The profile is the one place that decides two things the user can feel:

* the process priority class a launch gets (above normal by default — high can
  starve the desktop on a small machine), and
* the numerical thread budget every BLAS/OpenMP library reads at import time.

Both are persisted in ``config/api_keys.json`` and overridable per run. These
tests pin the precedence, the merge-safe write, and the exact environment
contract ``start_jarvis`` relies on.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import performance_profile as pp  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(pp._ENV_PRIORITY, raising=False)
    monkeypatch.delenv(pp._ENV_THREAD_CAP, raising=False)


# ── reading and precedence ──────────────────────────────────────────────────

def test_defaults_are_above_normal_and_automatic(tmp_path):
    profile = pp.read_profile(tmp_path / "missing.json")
    assert profile == {"cpu_priority": "above_normal", "thread_cap": 0}


def test_the_config_value_is_used_when_present(tmp_path):
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"performance": {
        "cpu_priority": "high", "thread_cap": 6}}), encoding="utf-8")
    assert pp.read_profile(path) == {"cpu_priority": "high", "thread_cap": 6}


def test_environment_overrides_the_file_for_one_run(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"performance": {
        "cpu_priority": "idle", "thread_cap": 6}}), encoding="utf-8")
    monkeypatch.setenv(pp._ENV_PRIORITY, "below normal")
    monkeypatch.setenv(pp._ENV_THREAD_CAP, "3")
    assert pp.read_profile(path) == {"cpu_priority": "below_normal",
                                     "thread_cap": 3}


def test_junk_values_fall_back_instead_of_guessing(tmp_path):
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"performance": {
        "cpu_priority": "ludicrous", "thread_cap": "many"}}), encoding="utf-8")
    assert pp.read_profile(path) == {"cpu_priority": "above_normal",
                                     "thread_cap": 0}


def test_a_corrupt_config_is_not_fatal(tmp_path):
    path = tmp_path / "api_keys.json"
    path.write_text("{not json", encoding="utf-8")
    assert pp.read_profile(path)["cpu_priority"] == "above_normal"


# ── writing ────────────────────────────────────────────────────────────────

def test_writing_preserves_every_other_key_and_is_atomic(tmp_path):
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({
        "gemini_api_key": "secret",
        "features": {"laya_ai": True},
        "performance": {"cpu_priority": "normal"},
    }), encoding="utf-8")

    assert pp.write_profile({"cpu_priority": "high", "thread_cap": 2}, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["gemini_api_key"] == "secret"
    assert data["features"] == {"laya_ai": True}
    assert data["performance"] == {"cpu_priority": "high", "thread_cap": 2}
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be renamed away"


def test_a_bad_priority_is_normalised_on_write(tmp_path):
    path = tmp_path / "api_keys.json"
    assert pp.write_profile({"cpu_priority": "URGENT", "thread_cap": 99}, path)
    assert json.loads(path.read_text(encoding="utf-8"))["performance"] == {
        "cpu_priority": "above_normal", "thread_cap": 0}


# ── the thread environment ─────────────────────────────────────────────────

def test_auto_cap_is_half_the_cores_with_a_ceiling():
    assert pp.default_thread_cap(4) == 2
    assert pp.default_thread_cap(16) == 8
    assert pp.default_thread_cap(1) == 1
    assert pp.default_thread_cap(64) == pp.AUTO_THREAD_MAX


def test_thread_environment_caps_every_numerical_library():
    env = pp.thread_environment({"cpu_priority": "normal", "thread_cap": 3})
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        assert env[name] == "3", name
    assert env["OMP_WAIT_POLICY"] == "PASSIVE"
    assert env["TOKENIZERS_PARALLELISM"] == "false"


def test_automatic_thread_cap_uses_the_machine():
    env = pp.thread_environment({"thread_cap": 0}, cpu_count=8)
    assert env["OMP_NUM_THREADS"] == "4"


def test_describe_reports_the_real_numbers():
    line = pp.describe({"cpu_priority": "above_normal", "thread_cap": 4})
    assert "above normal" in line and "4" in line


# ── the OS call ────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="priority classes are Windows-specific")
def test_priority_round_trips_on_windows():
    before = pp.current_priority() or "normal"
    try:
        ok, detail = pp.apply_priority("above_normal")
        assert ok is True, detail
        assert pp.current_priority() == "above_normal"
    finally:
        pp.apply_priority(before)


def test_an_unknown_priority_uses_the_safe_default():
    # normalize is what apply_priority feeds the OS; never an unvalidated name.
    assert pp.normalize_priority("realtime") == "above_normal"
    assert pp.normalize_priority("LOW") == "below_normal"
    assert pp.normalize_priority(None) == "above_normal"


def test_the_profile_module_is_stdlib_only():
    """start_jarvis loads it by path on the bare interpreter — no third-party."""
    import ast

    source = ROOT / "core" / "performance_profile.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    assert sorted(imported - allowed) == []
