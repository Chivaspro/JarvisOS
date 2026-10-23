"""File-attach failures must produce VISIBLE feedback.

``import_file`` reported problems through ``append_msg('FILE'/'ERR', ...)``,
but both append_msg implementations drop every identity that is not the user
or the assistant — by design ("FILE / ERR ... remain internal activity only").
The result: dropping a folder onto the chat, or a failing import, produced
ZERO feedback anywhere. Failures now go to the host's activity log, the
accepted channel for internal notes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

from .test_camera_pipeline import _pump, camera, qapp, ui  # noqa: F401,E402
from .test_image_view_panel import window  # noqa: F401,E402


def _open_chat(window, qapp):
    window._toggle_command_window()
    _pump(qapp, 0.05)
    win = getattr(window, "_command_window", None)
    assert win is not None, "the chat window did not open"
    return win


def test_dropping_a_folder_reports_why_it_did_not_attach(window, qapp, monkeypatch, tmp_path):
    win = _open_chat(window, qapp)
    logs = []
    monkeypatch.setattr(window, "write_log", lambda text: logs.append(str(text)))

    folder = tmp_path / "a_folder"
    folder.mkdir()
    win.import_file(str(folder))

    assert any("Could not attach" in line and "not a file" in line for line in logs), (
        f"the refusal produced no visible feedback; log was: {logs!r}")


def test_a_host_side_import_failure_is_reported(window, qapp, monkeypatch, tmp_path):
    win = _open_chat(window, qapp)
    logs = []
    monkeypatch.setattr(window, "write_log", lambda text: logs.append(str(text)))

    real = tmp_path / "file.txt"
    real.write_text("x", encoding="utf-8")

    def _boom(path):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(window, "import_file", _boom)
    win.import_file(str(real))

    assert any("Import failed" in line and "disk on fire" in line for line in logs), (
        f"the failure produced no visible feedback; log was: {logs!r}")
