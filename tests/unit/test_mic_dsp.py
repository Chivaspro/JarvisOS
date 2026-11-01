"""Microphone DSP: every control must change the real input signal.

These tests use synthetic blocks and inspect the samples :class:`MicDSP`
returns — the same samples ``main.py`` hands to the Live session, the voice
gate, the wake-word feed and the HUD meter. They pin the guarantees that make
Settings ▸ VOICE ▸ MICROPHONE INPUT honest:

* gain beyond 100 % really amplifies,
* noise suppression lowers steady noise energy,
* the gate closes on silence and opens on speech,
* the compressor reduces loud blocks,
* the limiter never lets a block clip or wrap int16,
* disabling every filter returns the input untouched.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.mic_dsp import MicDSP, MicSettings  # noqa: E402

SR = 16000
BLOCK = 1024


def _tone(amplitude: float, freq: float = 220.0, blocks: int = 1,
          phase: float = 0.0, rng: np.random.Generator | None = None):
    frames = BLOCK * blocks
    t = np.arange(frames) / SR
    sig = amplitude * np.sin(2 * math.pi * freq * t + phase)
    if rng is not None:
        sig = sig + rng.normal(0.0, 0.05, frames)
    return sig.astype(np.float32)


def _to_i16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(np.asarray(x, dtype=np.float32) * 32767.0),
                   -32768, 32767).astype(np.int16)


def _rms(x: np.ndarray) -> float:
    a = np.asarray(x, dtype=np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def _settings(**kw) -> MicSettings:
    base = dict(device="", gain_percent=100.0, noise_suppression=False,
                suppression_amount=0.0, gate=False, gate_db=-40.0,
                compressor=False, compressor_amount=0.0,
                limiter=False, ceiling_db=-1.0, monitoring=False)
    base.update(kw)
    return MicSettings(**base)


def test_gain_above_one_hundred_percent_really_amplifies():
    block = _to_i16(_tone(0.20))
    dsp = MicDSP(_settings(gain_percent=200.0), sample_rate=SR)
    out = dsp.process(block)
    assert out.dtype == np.int16
    # 200 % doubles the integer samples (well inside int16 range).
    assert abs(_rms(out) - 2.0 * _rms(block)) < 120.0


def test_gain_below_one_hundred_percent_attenuates():
    block = _to_i16(_tone(0.40))
    dsp = MicDSP(_settings(gain_percent=25.0), sample_rate=SR)
    out = dsp.process(block)
    assert _rms(out) < _rms(block) * 0.35


def test_noise_suppression_reduces_steady_noise_energy():
    rng = np.random.default_rng(1234)
    noise = _to_i16(rng.normal(0.0, 0.05, BLOCK).astype(np.float32))
    dsp = MicDSP(_settings(noise_suppression=True, suppression_amount=0.9),
                 sample_rate=SR)
    # Let the floor learn the noise, exactly as during a quiet room.
    for _ in range(12):
        out = dsp.process(noise)
    assert _rms(out) < _rms(noise) * 0.9, \
        "steady noise must be measurably quieter after suppression"


def test_gate_closes_on_silence_and_opens_on_speech():
    silence = np.zeros(BLOCK, dtype=np.int16)
    speech = _to_i16(_tone(0.30))
    dsp = MicDSP(_settings(gate=True, gate_db=-40.0), sample_rate=SR)
    # Closed by construction: silence stays silent.
    assert np.max(np.abs(dsp.process(silence))) <= 1
    # A real speech-level block opens it.
    for _ in range(3):
        out = dsp.process(speech)
    assert dsp._gate_open is True
    assert _rms(out) > 1000.0
    # Release eventually closes the gate again.
    for _ in range(40):
        out = dsp.process(silence)
    assert dsp._gate_open is False
    assert _rms(out) < 1.0


def test_compressor_reduces_a_loud_block():
    loud = _to_i16(_tone(0.50))
    dsp = MicDSP(_settings(compressor=True, compressor_amount=0.5),
                 sample_rate=SR)
    out = loud
    for _ in range(24):          # let the attack settle
        out = dsp.process(loud)
    assert _rms(out) < _rms(loud) * 0.9, \
        "a block above the threshold must be compressed, not boosted"


def test_limiter_never_clips_even_with_full_gain():
    # 400 % gain on a near-full-scale tone would clip hard without a limiter.
    block = _to_i16(_tone(1.0))
    dsp = MicDSP(_settings(gain_percent=400.0, limiter=True, ceiling_db=-1.0),
                 sample_rate=SR)
    out = dsp.process(block)
    ceiling = 10.0 ** (-1.0 / 20.0)
    assert _rms(out) / 32767.0 <= ceiling + 0.001
    assert out.max() < 32767
    assert out.min() > -32768
    assert not np.any(np.abs(out.astype(np.int32)) > 32767)


def test_passthrough_is_exact_when_every_filter_is_off():
    block = _to_i16(_tone(0.35))
    dsp = MicDSP(_settings(), sample_rate=SR)
    out = dsp.process(block)
    assert np.array_equal(out, block)


def test_defaults_match_the_settings_surface():
    s = MicSettings()
    assert s.gain_percent == 100.0
    assert s.noise_suppression is True
    assert s.limiter is True
    assert 0.0 <= s.suppression_amount <= 1.0
    assert -80.0 <= s.gate_db <= -5.0
    assert -12.0 <= s.ceiling_db <= 0.0


def test_monitor_reports_a_real_failure_instead_of_raising(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("sounddevice")

    def _boom(**_kw):
        raise RuntimeError("device busy")

    fake.InputStream = _boom
    fake.OutputStream = _boom
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    from voice.mic_dsp import MicMonitor
    monitor = MicMonitor(_settings(), sample_rate=SR)
    assert monitor.start() is False
    assert "device busy" in monitor.error
    assert monitor.running is False


def test_monitor_routes_the_processed_signal_to_the_output(monkeypatch):
    """One input stream, one output stream, and the processed audio is what is
    played back — the wiring TEST INPUT relies on."""
    import sys
    import types

    captured: dict = {}

    class _FakeStream:
        def __init__(self, kind, **kw):
            self.kind = kind
            self.callback = kw.get("callback")
            captured[kind] = self

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    fake = types.ModuleType("sounddevice")
    fake.InputStream = lambda **kw: _FakeStream("input", **kw)
    fake.OutputStream = lambda **kw: _FakeStream("output", **kw)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    from voice.mic_dsp import MicMonitor
    monitor = MicMonitor(_settings(gain_percent=200.0), sample_rate=SR,
                         block_size=BLOCK)
    assert monitor.start() is True
    loud = _to_i16(_tone(0.30))
    captured["input"].callback(loud, BLOCK, None, None)
    out = np.zeros((BLOCK, 1), dtype=np.int16)
    captured["output"].callback(out, BLOCK, None, None)
    assert int(np.max(np.abs(out))) > 0, "processed audio must reach the output"
    assert monitor.level > 0.0
    monitor.stop()
    assert monitor.running is False


def test_settings_signature_changes_with_a_real_control(monkeypatch):
    values = {"mic_gain_percent": 100, "mic_noise_suppression": True,
              "mic_noise_gate": False, "mic_compressor": False,
              "mic_limiter": True, "mic_limiter_ceiling_db": -1.0,
              "mic_noise_suppression_amount": 60,
              "mic_compressor_amount": 50, "mic_noise_gate_db": -45,
              "mic_monitoring": False, "input_device": ""}

    def fake_get(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr("config.settings.get", fake_get)
    a = MicSettings.from_config()
    assert a.gain_percent == 100.0
    assert a.suppression_amount == pytest.approx(0.6)
    assert a.compressor_amount == pytest.approx(0.5)

    values["mic_gain_percent"] = 175
    b = MicSettings.from_config()
    assert b.gain_percent == 175.0
    assert b.signature() != a.signature()
