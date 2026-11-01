"""Settings ▸ PLUGINS: real detail, settings, limits and persistence.

The tab is built from the live registry (or a local scan when JARVIS is not
running), shows each plugin's description/icon/status, renders the manifest's
typed settings and execution limits, and writes what it saves into the config
file the window was opened against.
"""
from __future__ import annotations

import json
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

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QLabel  # noqa: E402

from .qt_harness import config_file, qapp, settings_window  # noqa: F401,E402


def _rows(win) -> dict:
    out = {}
    for i in range(win._plugin_list.count()):
        data = win._plugin_list.item(i).data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            out[str(data.get("name"))] = i
    return out


def _select(win, name: str) -> bool:
    rows = _rows(win)
    if name not in rows:
        return False
    win._plugin_list.setCurrentRow(rows[name])
    return True


def _detail_text(win) -> str:
    return "\n".join(
        w.text() for w in win._plugin_detail.findChildren(QLabel) if w.text())


def test_builtins_are_listed_with_status_and_detail(settings_window, qapp):
    win = settings_window
    win._reload_plugins(force=True)
    qapp.processEvents()
    rows = _rows(win)
    assert "research" in rows and "seo_analyzer" in rows
    assert _select(win, "research")
    qapp.processEvents()
    # The manifest's fields are real editors on the detail panel.
    assert "max_results" in win._plugin_field_widgets
    assert "include_youtube" in win._plugin_field_widgets
    assert "research" in _detail_text(win).lower()


def test_saving_plugin_settings_writes_to_the_windows_config(
        settings_window, config_file, qapp):
    win = settings_window
    win._reload_plugins(force=True)
    assert _select(win, "research")
    qapp.processEvents()
    _ftype, widget = win._plugin_field_widgets["max_results"]
    widget.setValue(9)
    win._save_plugin_settings()
    stored = json.loads(Path(config_file).read_text(encoding="utf-8"))
    assert stored["plugin_settings"]["research"]["max_results"] == 9


def test_advanced_settings_carry_the_execution_limits(settings_window, qapp):
    win = settings_window
    win._reload_plugins(force=True)
    assert _select(win, "automation")
    qapp.processEvents()
    assert "max_calls_per_minute" in win._plugin_limits_widgets
    assert "timeout_seconds" in win._plugin_limits_widgets
    assert win._plugin_advanced is False
    win._toggle_plugin_advanced(True)
    qapp.processEvents()
    assert win._plugin_advanced is True
    win._plugin_limits_widgets["timeout_seconds"].setValue(120)
    win._save_plugin_settings()
    stored = json.loads(Path(win._config_path).read_text(encoding="utf-8"))
    assert stored["plugin_limits"]["automation"]["timeout_seconds"] == 120


def test_toggle_writes_the_enabled_flag(settings_window, config_file, qapp):
    win = settings_window
    win._reload_plugins(force=True)
    assert _select(win, "seo_analyzer")
    qapp.processEvents()
    win._toggle_plugin()
    stored = json.loads(Path(config_file).read_text(encoding="utf-8"))
    assert stored["plugins_enabled"]["seo_analyzer"] is False
