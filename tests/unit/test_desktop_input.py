"""Desktop input reports the window it acted on and verifies what it can.

Physical input cannot read back an arbitrary text field, so the honest contract
is narrower than "the text is in the app": refuse when no window has focus,
name the foreground window in the result, and verify the clipboard path that
carries the text. These tests pin that contract without moving the real pointer.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capabilities import desktop  # noqa: E402


class _PyAutoGUI:
    FAILSAFE = True
    PAUSE = 0.05

    def __init__(self):
        self.hotkeys = []
        self.typed = []

    def hotkey(self, *keys):
        self.hotkeys.append(keys)

    def write(self, text, interval=0.02):
        self.typed.append(text)

    def press(self, key):
        self.hotkeys.append((key,))


@pytest.fixture()
def fake_input(monkeypatch):
    fake = _PyAutoGUI()
    monkeypatch.setattr(desktop, "_pyautogui", lambda: fake)
    monkeypatch.setattr(desktop, "_foreground_title", lambda: "Untitled - Notepad")
    return fake


def test_typing_names_the_focused_window_and_verifies_the_clipboard(
        fake_input, monkeypatch):
    clipboard = {"value": ""}

    class _Clip:
        @staticmethod
        def copy(text):
            clipboard["value"] = text

        @staticmethod
        def paste():
            return clipboard["value"]

    monkeypatch.setitem(sys.modules, "pyperclip", _Clip)

    out = desktop.run("keyboard_type", {"text": "hello world"})

    assert out["ok"] is True
    assert out["evidence"]["window"] == "Untitled - Notepad"
    assert out["evidence"]["method"] == "clipboard"
    assert out["evidence"]["verified"] is True
    assert fake_input.hotkeys == [("ctrl", "v")]
    assert "into 'Untitled - Notepad'" in out["detail"]


def test_the_keyboard_fallback_is_reported_as_unverified(fake_input, monkeypatch):
    class _BrokenClip:
        @staticmethod
        def copy(text):
            raise RuntimeError("clipboard locked")

    monkeypatch.setitem(sys.modules, "pyperclip", _BrokenClip)

    out = desktop.run("keyboard_type", {"text": "hello"})

    assert out["ok"] is True
    assert out["evidence"]["method"] == "keyboard"
    assert out["evidence"]["verified"] is False
    assert fake_input.typed == ["hello"]


def test_typing_without_a_focused_window_is_refused(fake_input, monkeypatch):
    monkeypatch.setattr(desktop, "_foreground_title", lambda: "")
    out = desktop.run("keyboard_type", {"text": "hello"})
    assert out["ok"] is False
    assert "nothing to type into" in out["detail"]
    assert fake_input.typed == [] and fake_input.hotkeys == []


def test_a_hotkey_reports_the_window_it_pressed_into(fake_input, monkeypatch):
    out = desktop.run("keyboard_hotkey", {"keys": "ctrl s"})
    assert out["ok"] is True
    assert out["evidence"]["window"] == "Untitled - Notepad"
    assert out["evidence"]["keys"] == ["ctrl", "s"]
