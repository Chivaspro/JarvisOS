"""Display paths stay silent: previews and execution reports are never voiced.

The Chat Log shows previews, Markdown results, coding cards and operation cards.
Those are display surfaces — they must keep rendering exactly as before while
making ZERO speech calls. The voice only ever speaks a conversational reply; a
preview with complete sentences must not be read aloud.

These tests drive the REAL offscreen ``MainWindow`` (offscreen Qt; WebEngine
disabled, exactly like the boot smoke test) and install tripwires on the only
ways this app can produce sound:

* ``voice.speech_stream.StreamingSpeechService.feed`` — the live TTS sink;
* ``core.tts.create_tts_player`` — the factory every ``speak_local`` needs;
* ``ui.MainWindow.speak_local`` / ``ui.MainWindow.speak`` — tripwires: the UI
  layer has no speech API today (that is the point), so any future wiring that
  gives it one is caught here instead of in production.

Each test also proves the DISPLAY still happened (one card row, rendered bold /
headings), so the isolation cannot be achieved by gating the chat itself.
"""
from __future__ import annotations

import importlib.util
import os
import sys
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

# Load the app module under an alias (tests/ui is itself a package named 'ui').
_ui_spec = importlib.util.spec_from_file_location(
    "ui_impl_preview_speech_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)

UI_SRC = (ROOT / "ui.py").read_text(encoding="utf-8")

# A realistic result: real sentences, headings and bold — exactly the content
# that used to be at risk of being read aloud.
MARKDOWN = (
    "# Fusion preview\n\n"
    "The reactor design uses superconducting magnets. "
    "The team expects a net-energy demonstration in 2032.\n\n"
    "## Key points\n\n"
    "- The **magnet upgrade** is the critical path.\n"
    "- Fuel handling remains unverified.\n"
)
CODING_DONE = {
    "summary": "Built pong — runs and exits cleanly.",
    "success": True,
    "files": ["main.py", "utils.py"],
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A throwaway conversation store: a UI test must not write the real one."""
    from memory import conversations

    instance = conversations.ConversationStore(tmp_path / "conversations.db")
    monkeypatch.setattr(conversations, "get_store", lambda *a, **k: instance)
    yield instance
    instance.close()


@pytest.fixture()
def win(qapp, tmp_path, store):
    """The real HUD offscreen, with its Chat Panel open (the display target)."""
    window = ui.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()
    window._toggle_command_window()
    deadline = time.monotonic() + 20.0
    while window._command_window is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert window._command_window is not None, "the chat panel must open"
    qapp.processEvents()
    yield window
    try:
        window._shutdown_camera_threads(timeout=5.0)
    except Exception:
        pass
    try:
        panel = getattr(window, "_command_window", None)
        if panel is not None:
            panel.close()
    except Exception:
        pass
    try:
        window.close()
    except Exception:
        pass
    qapp.processEvents()


def _install_speech_spies(monkeypatch):
    """Tripwire every path that can make a sound; return the recorded calls."""
    calls: list[tuple] = []

    from voice import speech_stream

    def _sink_feed(self, text):
        calls.append(("StreamingSpeechService.feed", str(text)))
        return 0

    monkeypatch.setattr(speech_stream.StreamingSpeechService, "feed", _sink_feed)

    import core.tts as core_tts

    def _factory(cfg):
        calls.append(("create_tts_player", None))
        return None

    monkeypatch.setattr(core_tts, "create_tts_player", _factory)

    # The window has no speech API today; these spies make a regression loud.
    monkeypatch.setattr(ui.MainWindow, "speak_local",
                        lambda self, *a, **k: calls.append(("ui.speak_local", a)),
                        raising=False)
    monkeypatch.setattr(ui.MainWindow, "speak",
                        lambda self, *a, **k: calls.append(("ui.speak", a)),
                        raising=False)
    return calls


def _markdown_cards(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatMarkdownCard)]


def _coding_cards(panel):
    return [r for r in panel.chat._rows if isinstance(r, ui._ChatCodingCard)]


def _pump(qapp, seconds: float = 0.05) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


# ── the display surfaces ────────────────────────────────────────────────────

def test_show_content_renders_the_preview_and_never_speaks(qapp, win, monkeypatch):
    calls = _install_speech_spies(monkeypatch)
    panel = win._command_window
    before = len(_markdown_cards(panel))

    win.show_content("Fusion preview", MARKDOWN)
    _pump(qapp)

    cards = _markdown_cards(panel)
    assert len(cards) == before + 1, "the preview must appear as ONE chat card"
    card = cards[-1]
    assert card._title_text == "Fusion preview"
    assert "superconducting magnets" in card._preview.toPlainText(), (
        "the preview content must reach the Chat Log")

    # Markdown rendering survives: a real heading block and a bold run.
    rendered = card._preview.toHtml()
    assert "<h1" in rendered, f"the heading was not rendered: {rendered[:200]!r}"
    doc = card._preview.document()
    bold = doc.find("magnet upgrade")
    assert bold.hasSelection(), "the bold words must be in the rendered document"
    assert int(bold.charFormat().fontWeight()) >= 600, (
        "**bold** must still render as bold inside the preview card")

    assert calls == [], f"a preview must never reach the voice: {calls!r}"


def test_coding_activity_renders_one_card_and_never_speaks(qapp, win, monkeypatch):
    calls = _install_speech_spies(monkeypatch)
    panel = win._command_window
    before = len(_coding_cards(panel))

    win.coding_activity("start", {"description": "pong game", "project": "pong"})
    _pump(qapp)
    win.coding_activity("stage", {"stage": "Writing main.py..."})
    _pump(qapp)
    win.coding_activity("done", CODING_DONE)
    _pump(qapp)

    cards = _coding_cards(panel)
    assert len(cards) == before + 1, (
        "the whole coding session is ONE card, updated in place — not one per event")
    card = cards[-1]
    assert card.is_finished is True, "the done event must finalise the card"
    assert "runs and exits cleanly" in card.stage_text(), (
        "the execution report must reach the Chat Log")
    assert calls == [], f"an execution report must never be spoken: {calls!r}"


def test_task_operation_card_never_speaks(qapp, win, monkeypatch):
    """The operation/report card: a task step report is display content too."""
    calls = _install_speech_spies(monkeypatch)
    panel = win._command_window
    before = len([r for r in panel.chat._rows
                  if isinstance(r, ui._ChatOperationCard)])

    panel.apply_task_event_card({
        "id": "TASK-2026-000001",
        "title": "Search fusion news",
        "state": "RUNNING",
        "category": "SEARCH",
        "event": "step_started",
        "steps": [{"id": "s1", "title": "web_search · fusion",
                   "status": "running", "outcome": ""}],
        "answers": {}, "findings": [], "warnings": [], "errors": [],
        "artifacts": [], "questions_state": "pending",
    })
    _pump(qapp)

    cards = [r for r in panel.chat._rows
             if isinstance(r, ui._ChatOperationCard)]
    assert len(cards) == before + 1, (
        "the operation/report card must still be displayed in the Chat Log")
    assert calls == [], f"an operation report must never be spoken: {calls!r}"


# ── structural evidence ─────────────────────────────────────────────────────

def test_the_ui_layer_has_no_speech_api():
    """The HUD cannot speak: every TTS entry point lives in main.py's firewall."""
    assert not hasattr(ui.MainWindow, "speak_local")
    assert not hasattr(ui.MainWindow, "speak")
    assert not hasattr(ui.RefinedCommandWindow, "speak_local")
    assert not hasattr(ui.RefinedCommandWindow, "speak")


def _class_block(name: str, next_name: str) -> str:
    start = UI_SRC.index(f"class {name}")
    end = UI_SRC.index(f"class {next_name}", start)
    return UI_SRC[start:end]


def _method_block(marker: str) -> str:
    """One method body: from ``marker`` to the next same-indent ``def``."""
    start = UI_SRC.index(marker)
    body = UI_SRC[start + len(marker):]
    lines: list[str] = []
    for line in body.splitlines(keepends=True):
        if line.startswith("    def ") and lines:
            break
        lines.append(line)
    return "".join(lines)


def test_display_code_never_references_the_voice():
    """Static proof: no display surface even knows a speech system exists."""
    blocks = {
        "_ChatMarkdownCard": _class_block("_ChatMarkdownCard", "_ChatCodingCard"),
        "_ChatCodingCard": _class_block("_ChatCodingCard", "_ChatOperationCard"),
        "_ChatOperationCard": _class_block("_ChatOperationCard", "_RoundedChatLog"),
        "show_content": _method_block("def show_content(self, title, text):"),
    }
    for name, block in blocks.items():
        lowered = block.lower()
        for forbidden in ("speak", "speech", "tts", "audio", "voice"):
            assert forbidden not in lowered, (
                f"{name} refers to {forbidden!r}: display paths must not be "
                f"gated by the voice system")


def test_the_coding_and_operation_handlers_never_speak():
    # The live handlers on the chat panel (the RefinedCommandWindow overrides).
    start = UI_SRC.index("class RefinedCommandWindow")
    tail = UI_SRC[start:]
    for marker in ("def apply_coding_preview(self, kind: str, payload: dict | None = None) -> None:",
                   "def apply_task_event_card(self, payload: dict) -> None:"):
        block = _method_block_on(tail, marker).lower()
        for forbidden in ("speak", "speech", "tts", "audio"):
            assert forbidden not in block, (
                f"{marker} refers to {forbidden!r}: it must stay display-only")


def _method_block_on(source: str, marker: str) -> str:
    start = source.index(marker)
    body = source[start + len(marker):]
    lines: list[str] = []
    for line in body.splitlines(keepends=True):
        if line.startswith("    def ") and lines:
            break
        lines.append(line)
    return "".join(lines)