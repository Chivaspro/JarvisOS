"""Settings ▸ VIDEO: camera probing runs OFF the GUI thread.

A cold DirectShow open costs 8-20 s, and the old synchronous scan ran four of
them with the full 12-frame warm-up each inside ``_build_video_tab`` — opening
Settings froze the whole HUD for seconds. The scan now runs on a worker thread
(``settings-camera-scan``) and the result arrives through the
``camera_scan_found`` signal, which only ever ADDS picker entries.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402


def _build(qapp, tmp_path, data):
    import ui_settings
    p = tmp_path / "api_keys.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    win = ui_settings.SettingsWindow(config_path=p)
    win.show()
    qapp.processEvents()
    return win


def _pump_until(qapp, cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        qapp.processEvents()
        time.sleep(0.01)
    return cond()


def _fake_probe(monkeypatch, lit_index):
    calls = []
    import vision.camera as cam

    def _probe(index, backend=None, quick=False):
        calls.append((index, threading.current_thread().name, bool(quick)))
        return index == lit_index

    monkeypatch.setattr(cam, "probe_camera", _probe)
    return calls


def test_probing_never_runs_on_the_gui_thread(qapp, tmp_path, monkeypatch):
    calls = _fake_probe(monkeypatch, lit_index=3)
    win = _build(qapp, tmp_path, {"camera_index": "1"})
    try:
        main = threading.main_thread().name
        assert all(name != main for _i, name, _q in calls), (
            f"a camera probe ran on the GUI thread during construction: {calls}")
        # The worker's short-burst variant is what runs.
        assert _pump_until(qapp, lambda: any(
            name.startswith("settings-camera-scan") for _i, name, _q in calls))
        assert all(quick for _i, _n, quick in calls
                   if _n.startswith("settings-camera-scan")), (
            "the discovery scan must use the quick (2-read) probe variant")
    finally:
        win.close()


def test_a_found_device_is_added_and_the_saved_choice_survives(qapp, tmp_path, monkeypatch):
    _fake_probe(monkeypatch, lit_index=3)
    win = _build(qapp, tmp_path, {"camera_index": "1"})
    try:
        combo = win.camera_source
        assert _pump_until(qapp, lambda: combo.findData("3") >= 0), (
            "the discovered device never reached the picker")
        assert str(combo.currentData()) == "1", (
            "a late discovery must not steal the saved selection")
    finally:
        win.close()


def test_a_dead_scan_changes_nothing(qapp, tmp_path, monkeypatch):
    _fake_probe(monkeypatch, lit_index=None)
    win = _build(qapp, tmp_path, {"camera_index": "auto"})
    try:
        combo = win.camera_source
        before = [combo.itemData(i) for i in range(combo.count())]
        # Give the worker a moment, then confirm no entries were removed or
        # added by a scan that found nothing.
        time.sleep(0.3)
        qapp.processEvents()
        after = [combo.itemData(i) for i in range(combo.count())]
        assert after == before == ["auto", "0", "1"]
    finally:
        win.close()
