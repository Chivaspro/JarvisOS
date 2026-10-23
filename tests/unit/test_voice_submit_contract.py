"""The finalized voice utterance goes through the ONE chat-submission path.

Pins the runtime contract that fixes the reported bug — a spoken command was
executed and answered but never appeared as a submitted user message:

  * the final transcript is submitted with ``ui.submit_chat_turn("user", ...)``
    (display + record, even with the panel hidden), not a log-string;
  * it is submitted BEFORE the reply line, so the conversation reads
    user-then-reply;
  * the submission is presentation only — the Live turn already executed the
    command, so nothing dispatchable is called again;
  * a duplicate final event for the same utterance is suppressed.

The live Gemini session cannot run headless, so the wiring is pinned in the
file that owns it — the same convention as ``test_realtime_stt_toggle.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
UI = (ROOT / "ui.py").read_text(encoding="utf-8")


def _final_turn_block() -> str:
    start = MAIN.index('full_in = " ".join(in_buf).strip()')
    end = MAIN.index("in_buf = []", start)
    return MAIN[start:end]


def test_final_utterance_submits_through_the_shared_path():
    block = _final_turn_block()
    assert 'self.ui.submit_chat_turn("user", full_in)' in block
    # The old log-string transport (fragile prefix parsing) is gone for the
    # user turn; the reply still travels through it.
    assert 'write_log(f"You: {full_in}")' not in block


def test_submission_precedes_the_reply_line():
    submit = MAIN.index('self.ui.submit_chat_turn("user", full_in)')
    reply = MAIN.index('self.ui.write_log(f"{self._asst_name}: {full_out}")')
    assert submit < reply, "the user turn must be recorded before its reply"


def test_submission_never_re_dispatches_the_command():
    block = _final_turn_block()
    for forbidden in ("send_client_content", "_send_backend_command",
                      "on_text_command", "_send_realtime"):
        assert forbidden not in block, \
            f"submitting the transcript must not call {forbidden}"


def test_a_duplicate_final_event_is_suppressed():
    block = _final_turn_block()
    assert "_dup_final" in block
    assert "_last_submitted_utterance" in block and "_last_submitted_at" in block
    # The submit sits inside the not-duplicate guard.
    guard = block.index("if not _dup_final:")
    assert block.index('self.ui.submit_chat_turn("user", full_in)') > guard


def test_the_ui_exposes_the_submission_api():
    assert "_chat_turn_sig = pyqtSignal(str, str)" in UI
    assert "def submit_chat_turn(self, role: str, text: str) -> None:" in UI
    assert "def _apply_chat_turn(self, role: str, text: str) -> None:" in UI
    assert "def submit_chat_turn(self, role: str, text: str):" in UI, \
        "the thread-safe backend proxy must expose it too"


def test_chat_routes_gate_on_existence_not_visibility():
    """A hidden chat panel must still receive and record turns."""
    start = UI.index("def _log_from_backend")
    end = UI.index("def _activity_add", start)
    block = UI[start:end]
    assert "if win is not None and win.isVisible():" not in block, \
        "the reply route must not require the panel to be on screen"
    assert "if win is not None:" in block

    for name in ("def begin_assistant_stream", "def _stream_assistant_text_ui",
                 "def finish_assistant_stream"):
        s = UI.index(name, UI.index("class MainWindow"))
        body = UI[s:s + 900]
        assert "_live_command_window()" in body, \
            f"{name} must use the existence-based window accessor"


def test_composer_draft_detection_uses_text_edited_and_the_echo_log():
    assert "textEdited.connect(self._on_composer_user_edit)" in UI
    assert "_speech_echo_log" in UI
    # The OLD focus-based guess must be gone.
    assert "box.hasFocus() and current and current not in echoes" not in UI
