"""How children launch: one explicit helper, one correct app-wide default.

``main.py`` used to replace ``subprocess.Popen`` itself, force
``CREATE_NO_WINDOW`` on every child and DROP any caller ``startupinfo``. These
tests pin the replacement: the helper is safe on every platform, the default
preserves a caller's own flags and startupinfo, and a deliberately visible
launch can opt out.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import process_launch as pl  # noqa: E402


# ── the per-call helper (the preferred pattern) ──────────────────────────────

def test_hidden_kwargs_adds_no_window_on_windows_only():
    kw = pl.hidden_kwargs()
    if sys.platform == "win32":
        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in kw


def test_hidden_kwargs_preserves_caller_flags_and_kwargs():
    kw = pl.hidden_kwargs(capture_output=True, creationflags=0x40)
    assert kw["capture_output"] is True
    if sys.platform == "win32":
        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kw["creationflags"] & 0x40


def test_visible_kwargs_never_hide():
    kw = pl.visible_kwargs(cwd="C:/tmp")
    assert "creationflags" not in kw and kw["cwd"] == "C:/tmp"


def test_run_hidden_runs_a_real_child_without_a_console():
    r = pl.run_hidden([sys.executable, "-c", "print('ok')"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "ok" in r.stdout


# ── the app-wide default (the backstop) ──────────────────────────────────────

def test_install_default_is_idempotent_and_detectable():
    # The default may already be installed by main.py (imported by other tests),
    # so this test installs over a known-plain Popen to be deterministic.
    original = subprocess.Popen

    class _Plain:
        def __init__(self, *a, **k):
            pass

    try:
        subprocess.Popen = _Plain
        pl._INSTALLED = False
        pl.install_default()
        first = subprocess.Popen
        pl.install_default()
        assert subprocess.Popen is first, "installing twice must be a no-op"
        if sys.platform == "win32":
            assert getattr(subprocess.Popen, "_jarvis_no_window_default", False)
            assert first is not _Plain, "the default must actually patch Popen"
    finally:
        subprocess.Popen = original
        pl._INSTALLED = False


def _install_over_fake(monkeypatch):
    """Install the default on top of a recording fake; returns the seen kwargs."""
    seen: dict = {}

    class _Fake:
        def __init__(self, args, **kw):
            seen.update(kw)

    monkeypatch.setattr(subprocess, "Popen", _Fake)
    pl._INSTALLED = False
    pl.install_default()
    return seen


def test_install_default_preserves_startupinfo_and_caller_flags(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("CREATE_NO_WINDOW is Windows-only")
    seen = _install_over_fake(monkeypatch)
    try:
        sentinel = object()
        subprocess.Popen(["x"], startupinfo=sentinel, creationflags=0x40)
        assert seen.get("startupinfo") is sentinel, "startupinfo must be preserved"
        assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert seen["creationflags"] & 0x40, "the caller's own flags must survive"
    finally:
        monkeypatch.undo()
        pl._INSTALLED = False


def test_install_default_can_be_bypassed_for_a_visible_launch(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("CREATE_NO_WINDOW is Windows-only")
    seen = _install_over_fake(monkeypatch)
    try:
        subprocess.Popen(["terminal"], visible=True)
        assert "visible" not in seen, "the marker is consumed, never forwarded"
        assert "creationflags" not in seen, "a visible launch keeps its console"
    finally:
        monkeypatch.undo()
        pl._INSTALLED = False


# ── the source must not go back to the inline monkeypatch ────────────────────

def test_main_no_longer_patches_popen_inline():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'kw.pop("startupinfo", None)' not in source
    assert "_subprocess.Popen = _Popen" not in source
    assert "process_launch.install_default()" in source
