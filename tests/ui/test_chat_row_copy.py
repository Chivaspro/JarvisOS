"""Copy actions on a chat message row (right-click menu).

Chat rows are painted widgets, so there is no native text selection. These
tests pin the two copy affordances added for that gap: copy the message, and
copy the whole conversation.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

from PyQt6.QtWidgets import QApplication  # noqa: E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_copy_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


def _log_with_rows(qapp):
    log = ui._RoundedChatLog()
    log.show()
    log.resize(420, 260)
    qapp.processEvents()
    log.append_message('You', 'open youtube', you=True, muted=True)
    log.append_message('J.A.R.V.I.S.', 'Opening YouTube now, sir.', muted=True)
    qapp.processEvents()
    rows = [r for r in log._rows if isinstance(r, ui._ChatMessageRow)]
    return log, rows


def test_copy_message_text(qapp):
    _log, rows = _log_with_rows(qapp)
    row = rows[0]
    assert row.copy_to_clipboard() is True
    assert QApplication.clipboard().text() == 'open youtube'


def test_copy_whole_conversation(qapp):
    _log, rows = _log_with_rows(qapp)
    assert rows[1]._copy_whole_conversation() is True
    text = QApplication.clipboard().text()
    assert 'You: open youtube' in text
    assert 'J.A.R.V.I.S.: Opening YouTube now, sir.' in text
    assert text.index('You:') < text.index('J.A.R.V.I.S.:')


def test_the_row_exposes_a_context_menu_with_copy_actions():
    src = (ROOT / 'ui.py').read_text(encoding='utf-8')
    start = src.index('def contextMenuEvent', src.index('class _ChatMessageRow'))
    block = src[start:start + 900]
    assert "menu.addAction('Copy message text')" in block
    assert "menu.addAction('Copy whole conversation')" in block
    assert 'def copy_to_clipboard(self)' in src
