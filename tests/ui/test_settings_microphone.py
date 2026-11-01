"""Settings ▸ VOICE ▸ MICROPHONE INPUT is wired to the real DSP.

The panel is not decoration: each control becomes a field on
:class:`voice.mic_dsp.MicSettings`, ``SAVE`` persists the keys the runtime
reads, and ``DEFAULTS`` restores them. These tests pin those three contracts
without opening an audio device (monitoring stays off, so nothing blocks).
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

from .qt_harness import qapp  # noqa: F401,E402


def _settings_window(tmp_path, data=None):
    import ui_settings
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps(data or {}), encoding="utf-8")
    return ui_settings.SettingsWindow(config_path=cfg)


def test_the_panel_builds_with_real_controls(tmp_path):
    win = _settings_window(tmp_path)
    try:
        for attr in ("mic_device", "mic_gain", "mic_ns", "mic_ns_amount",
                     "mic_gate", "mic_gate_db", "mic_comp", "mic_comp_amount",
                     "mic_limiter", "mic_ceiling", "mic_monitoring",
                     "mic_level"):
            assert hasattr(win, attr), f"missing control: {attr}"
        # Gain really goes beyond 100 %.
        assert win.mic_gain.minimum() == 0
        assert win.mic_gain.maximum() == 400
    finally:
        win.close()


def test_the_panel_maps_to_real_dsp_settings(tmp_path):
    win = _settings_window(tmp_path)
    try:
        win.mic_gain.setValue(250)
        win.mic_ns.setChecked(True)
        win.mic_ns_amount.setValue(80)
        win.mic_gate.setChecked(True)
        win.mic_gate_db.setValue(-30)
        win.mic_comp.setChecked(True)
        win.mic_comp_amount.setValue(70)
        win.mic_limiter.setChecked(False)
        win.mic_ceiling.setValue(-3)

        s = win._mic_settings_from_panel()
        assert s.gain_percent == 250.0
        assert s.noise_suppression is True
        assert s.suppression_amount == pytest.approx(0.8)
        assert s.gate is True and s.gate_db == -30.0
        assert s.compressor is True
        assert s.compressor_amount == pytest.approx(0.7)
        assert s.limiter is False and s.ceiling_db == -3.0
        # The values are clamped to the documented ranges.
        assert 0.0 <= s.suppression_amount <= 1.0
    finally:
        win.close()


def test_collect_carries_the_microphone_keys(tmp_path):
    win = _settings_window(tmp_path)
    try:
        win.mic_gain.setValue(180)
        win.mic_gate.setChecked(True)
        win.mic_gate_db.setValue(-35)
        win.mic_monitoring.setChecked(False)
        data = win._collect()
        assert data["mic_gain_percent"] == 180
        assert data["mic_noise_gate"] is True
        assert data["mic_noise_gate_db"] == -35
        assert data["mic_limiter"] is True
        assert data["mic_monitoring"] is False
    finally:
        win.close()


def test_save_persists_the_microphone_keys(tmp_path):
    win = _settings_window(tmp_path)
    try:
        win.mic_gain.setValue(275)
        win.mic_comp.setChecked(True)
        win.mic_comp_amount.setValue(65)
        win.save_settings()
        stored = json.loads((tmp_path / "api_keys.json").read_text(encoding="utf-8"))
        assert stored["mic_gain_percent"] == 275
        assert stored["mic_compressor"] is True
        assert stored["mic_compressor_amount"] == 65
        assert "input_device" in stored
    finally:
        win.close()


def test_reset_restores_the_shipped_microphone_defaults(tmp_path):
    win = _settings_window(tmp_path, {
        "mic_gain_percent": 300,
        "mic_noise_gate": True,
        "mic_compressor": True,
        "mic_limiter": False,
        "mic_monitoring": True,
    })
    try:
        assert win.mic_gain.value() == 300
        assert win.mic_gate.isChecked() is True
        assert win.mic_limiter.isChecked() is False

        win.reset_defaults()
        assert win.mic_gain.value() == 100
        assert win.mic_ns.isChecked() is True
        assert win.mic_gate.isChecked() is False
        assert win.mic_comp.isChecked() is False
        assert win.mic_limiter.isChecked() is True
        assert win.mic_monitoring.isChecked() is False
    finally:
        win.close()


def test_the_runtime_reads_exactly_what_the_panel_writes(tmp_path, monkeypatch):
    """SAVE → the config the DSP's ``from_config`` actually reads."""
    win = _settings_window(tmp_path)
    try:
        win.mic_device.setCurrentIndex(win.mic_device.findData(""))
        win.mic_gain.setValue(160)
        win.mic_ns.setChecked(False)
        win.mic_gate.setChecked(True)
        win.mic_gate_db.setValue(-38)
        win.mic_limiter.setChecked(True)
        win.mic_ceiling.setValue(-2)
        win.save_settings()
    finally:
        win.close()

    stored = json.loads((tmp_path / "api_keys.json").read_text(encoding="utf-8"))
    from config import settings as cfg
    monkeypatch.setattr(cfg, "get",
                        lambda key, default=None: stored.get(key, default))
    from voice.mic_dsp import MicSettings
    s = MicSettings.from_config()
    assert s.gain_percent == 160.0
    assert s.noise_suppression is False
    assert s.gate is True and s.gate_db == -38.0
    assert s.limiter is True and s.ceiling_db == -2.0
