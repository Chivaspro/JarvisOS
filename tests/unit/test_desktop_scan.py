"""Desktop scanning: real monitors in, real model answers out.

The screen tool used to capture only the first monitor and hand the image to a
later turn. These tests pin the replacement contract:

* every physical monitor is captured, labelled with its real geometry,
* each monitor's pixels go to the configured vision provider,
* the answer is returned in the same call, with the user's question attached,
* a missing capture backend / provider / privacy mode raises an actionable
  error instead of claiming the screen was analysed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import actions.screen_processor as sp  # noqa: E402


class _Shot:
    def __init__(self, width, height, colour):
        self.size = (width, height)
        self.rgb = bytes([colour]) * (width * height * 3)


class _Sct:
    def __init__(self, monitors):
        self.monitors = monitors

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, target):
        return _Shot(target["width"], target["height"], target.get("colour", 0))


def _fake_mss(monitors):
    import io

    from PIL import Image

    def _to_png(rgb, size):
        buf = io.BytesIO()
        Image.frombytes("RGB", size, rgb).save(buf, format="PNG")
        return buf.getvalue()

    module = types.SimpleNamespace()
    module.mss = lambda: _Sct(monitors)
    module.tools = types.SimpleNamespace(to_png=_to_png)
    return module


MONITORS = [
    {"left": 0, "top": 0, "width": 320, "height": 240, "colour": 10},
    {"left": 320, "top": 0, "width": 256, "height": 200, "colour": 20},
]


@pytest.fixture()
def desktop(monkeypatch):
    fake = _fake_mss([{"left": 0, "top": 0, "width": 576, "height": 240}] + MONITORS)
    monkeypatch.setattr(sp, "mss", fake, raising=False)
    monkeypatch.setattr(sp, "_MSS", True)
    return fake


def test_every_physical_monitor_is_captured_with_its_geometry(desktop):
    shots = sp.capture_desktop()
    assert [s["label"] for s in shots] == ["Monitor 1", "Monitor 2"]
    assert [(s["width"], s["height"]) for s in shots] == [(320, 240), (256, 200)]
    assert [(s["left"], s["top"]) for s in shots] == [(0, 0), (320, 0)]
    assert all(s["image"] for s in shots)


def test_the_virtual_desktop_is_only_a_fallback(monkeypatch):
    fake = _fake_mss([{"left": 0, "top": 0, "width": 320, "height": 240}])
    monkeypatch.setattr(sp, "mss", fake, raising=False)
    monkeypatch.setattr(sp, "_MSS", True)
    shots = sp.capture_desktop()
    assert len(shots) == 1 and shots[0]["label"] == "Monitor 1"


def test_a_missing_capture_backend_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(sp, "_MSS", False)
    with pytest.raises(RuntimeError) as excinfo:
        sp.capture_desktop()
    assert "mss" in str(excinfo.value)


class _Provider:
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def is_available(self):
        return True

    def analyze_image(self, frame, question):
        self.calls.append((frame.shape, question))
        return self.answers.pop(0)


def _patch_vision(monkeypatch, provider, *, local_only=False):
    import vision.config as vconfig
    import vision.multimodal as mm

    monkeypatch.setattr(vconfig, "load_vision_config",
                        lambda: types.SimpleNamespace(local_only=local_only))
    monkeypatch.setattr(mm, "get_vision_provider", lambda cfg: provider)


def test_analysis_runs_on_every_monitor_in_the_same_call(desktop, monkeypatch):
    provider = _Provider(["Code editor with a failing test.", "Browser open on docs."])
    _patch_vision(monkeypatch, provider)

    text = sp.analyze_desktop("What am I doing?")

    assert "Monitor 1 (320x240):" in text
    assert "Monitor 2 (256x200):" in text
    assert "Code editor with a failing test." in text
    assert "Browser open on docs." in text
    assert len(provider.calls) == 2
    # Each call carried the real frame shape and the user's question.
    assert provider.calls[0][0][:2] == (240, 320)
    assert provider.calls[1][0][:2] == (200, 256)
    assert all("What am I doing?" in q for _shape, q in provider.calls)


def test_a_single_monitor_answer_is_not_wrapped_in_labels(monkeypatch):
    fake = _fake_mss([{"left": 0, "top": 0, "width": 320, "height": 240}])
    monkeypatch.setattr(sp, "mss", fake, raising=False)
    monkeypatch.setattr(sp, "_MSS", True)
    provider = _Provider(["A terminal running pytest."])
    _patch_vision(monkeypatch, provider)
    text = sp.analyze_desktop("")
    assert text == "A terminal running pytest."


def test_an_unavailable_provider_is_an_error(desktop, monkeypatch):
    class _Down:
        def is_available(self):
            return False

    _patch_vision(monkeypatch, _Down())
    with pytest.raises(RuntimeError) as excinfo:
        sp.analyze_desktop("what is on screen")
    assert "No vision model" in str(excinfo.value)


def test_privacy_mode_refuses_before_capturing(desktop, monkeypatch):
    monkeypatch.setattr(sp, "capture_desktop",
                        lambda: (_ for _ in ()).throw(AssertionError("captured")))
    _patch_vision(monkeypatch, _Provider([]), local_only=True)
    with pytest.raises(RuntimeError) as excinfo:
        sp.analyze_desktop("what is on screen")
    assert "Privacy mode" in str(excinfo.value)


def test_a_provider_failure_names_the_monitor(desktop, monkeypatch):
    class _Broken(_Provider):
        def analyze_image(self, frame, question):
            raise RuntimeError("quota exceeded")

    _patch_vision(monkeypatch, _Broken([]))
    with pytest.raises(RuntimeError) as excinfo:
        sp.analyze_desktop("")
    assert "Monitor 1" in str(excinfo.value)
    assert "quota exceeded" in str(excinfo.value)
