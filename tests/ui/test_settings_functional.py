"""The "Show in chat" preset and the "Auto-save remembered facts" switch.

Two controls were saved to the config but read by nothing, so they only *looked*
functional:

* Settings ▸ SYSTEM ▸ CHAT LOG ▸ "Show in chat": the combobox wrote its own
  string key, which no consumer reads. The log filter is driven by the
  individual category checkboxes, so picking a preset changed nothing.

* Settings ▸ GENERAL ▸ "Auto-save remembered facts to memory/long_term.json":
  the flag was never consulted by the memory store.

Both now have a real, verified effect.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── the chat-filter preset (needs Qt) ────────────────────────────────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")


def _settings_window(tmp_path, data=None):
    import json
    import ui_settings
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps(data or {}), encoding="utf-8")
    return ui_settings.SettingsWindow(config_path=cfg)


def test_chat_preset_all_checks_everything(tmp_path):
    win = _settings_window(tmp_path, {"chat_filter": "all"})
    try:
        win.chat_filter.setCurrentIndex(win.chat_filter.findData("chat"))
        assert win.chat_category_checks["sys"].isChecked() is False
        win.chat_filter.setCurrentIndex(win.chat_filter.findData("all"))
        assert all(cb.isChecked() for cb in win.chat_category_checks.values())
    finally:
        win.close()


def test_chat_preset_chat_hides_system_noise(tmp_path):
    win = _settings_window(tmp_path)
    try:
        idx = win.chat_filter.findData("chat")
        assert idx >= 0
        win.chat_filter.setCurrentIndex(idx)
        checks = win.chat_category_checks
        assert checks["jarvis"].isChecked() is True
        assert checks["you"].isChecked() is True
        assert checks["sys"].isChecked() is False
        assert checks["vis"].isChecked() is False
    finally:
        win.close()


def test_chat_preset_system_mode_shows_only_system(tmp_path):
    win = _settings_window(tmp_path)
    try:
        win.chat_filter.setCurrentIndex(win.chat_filter.findData("system"))
        checks = win.chat_category_checks
        assert checks["sys"].isChecked() is True
        assert checks["jarvis"].isChecked() is False
        assert checks["you"].isChecked() is False
    finally:
        win.close()


# ── the auto-save switch (no Qt) ─────────────────────────────────────────────

def test_autosave_off_stops_automatic_capture_but_not_explicit_commands(monkeypatch, tmp_path):
    from memory import manager as brain_manager
    from memory import database as brain_db
    from memory import backend as memory_backend
    from memory import memory_manager as mm

    # Isolated DB + no vault backend, mirroring the memory test conventions.
    monkeypatch.setattr(brain_db, "DEFAULT_DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("JARVIS_BRAIN_DB", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    monkeypatch.setattr(memory_backend, "selected_backend",
                        lambda: memory_backend.BACKEND_ORIGINAL)
    monkeypatch.setattr(memory_backend, "_mem0_configured", lambda: False)
    memory_backend.reset_memory_backend()
    brain_manager._instances.clear()
    mm._memory_cache.clear()

    from config import settings as cfg
    _real_get = cfg.get
    monkeypatch.setattr(cfg, "get", lambda key, default=None: (
        False if key == "memory_autosave" else _real_get(key, default)))

    brain = brain_manager.get_brain_memory()

    # Automatic capture is refused…
    assert brain.remember("my favourite colour is teal", explicit=False) is None
    # …while an explicit command is still honoured.
    assert brain.remember("the wifi password hint is on the fridge", explicit=True) is not None

    brain_manager._instances.clear()
    memory_backend.reset_memory_backend()
    mm._memory_cache.clear()


def test_autosave_default_is_on_when_the_setting_cannot_be_read(monkeypatch):
    from memory.manager import BrainMemory
    from config import settings as cfg

    def _boom(key, default=None):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(cfg, "get", _boom)
    obj = BrainMemory.__new__(BrainMemory)
    assert obj.autosave_enabled() is True


# ── the always-on-top preference is honoured (source contract) ───────────────

def test_panel_always_on_top_is_wired():
    ui_src = (ROOT / "ui.py").read_text(encoding="utf-8")
    assert "self._panel_always_on_top = bool(cfg.get('panel_always_on_top', True))" in ui_src
    start = ui_src.index("def _set_panel_visible")
    block = ui_src[start:start + 1600]
    assert "panel.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint" in block
    assert "bool(getattr(self, '_panel_always_on_top', True))" in block
    assert "self._panel_always_on_top = bool(data.get('panel_always_on_top', True))" in ui_src

