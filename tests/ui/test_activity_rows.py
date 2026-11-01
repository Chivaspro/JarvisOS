"""Activity rows: only live work is shown, expansion really reflows the card.

The dashboard rows used to be nine permanently visible 24-pixel strips with a
hard-coded 125-pixel expanded state. These tests pin the replacement:

* a row is hidden until a real activity starts, and hidden again when it ends,
* the active accent is green (the real "running" signal),
* expanding measures the content and pushes the following row down,
* collapsing returns to the header height without leaving a gap.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("PyQt6")

from .qt_harness import qapp  # noqa: F401  (fixture)

# ``tests/ui`` is a package named ``ui``, so load the app module under an alias.
_ui_spec = importlib.util.spec_from_file_location("ui_impl_activity_tests", ROOT / "ui.py")
_ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = _ui
_ui_spec.loader.exec_module(_ui)

from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QVBoxLayout, QWidget  # noqa: E402

C = _ui.C
_ActivityRow = _ui._ActivityRow


def test_a_row_is_revealed_by_real_activity_and_hidden_when_it_ends(qapp):
    row = _ActivityRow("SEARCH")
    try:
        assert row.isHidden(), "an idle row must not occupy the card"
        row.set_activity_detail("Searching the web for tariffs")
        assert not row.isHidden()
        assert C.GREEN in row.category_label.styleSheet()
        assert row.detail_label.text().startswith("Searching")
        row.set_inactive()
        assert row.isHidden()
        assert row.full_detail() == "Searching the web for tariffs"
    finally:
        row.deleteLater()


def test_expansion_reflows_the_following_row_and_collapses_cleanly(qapp):
    host = QWidget()
    host.resize(220, 420)
    lay = QVBoxLayout(host)
    first, second = _ActivityRow("CODE"), _ActivityRow("REVIEW")
    lay.addWidget(first)
    lay.addWidget(second)
    lay.addStretch(1)
    host.show()
    qapp.processEvents()
    try:
        first.set_activity_detail("Writing the parser module")
        second.set_activity_detail("Reviewing the diff")
        host.adjustSize()
        qapp.processEvents()
        before = second.geometry().top()

        first.toggle_expanded()
        QTest.qWait(260)
        qapp.processEvents()
        after = second.geometry().top()

        assert first._expanded is True
        assert after > before, "the following row did not move down"
        assert first.height() > 24
        assert first.height() <= 240

        first.toggle_expanded()
        QTest.qWait(260)
        qapp.processEvents()
        assert first._expanded is False
        assert first.height() == 24
        assert second.geometry().top() == before
    finally:
        host.deleteLater()


def test_expanded_height_is_measured_capped_and_scrolls(qapp):
    row = _ActivityRow("ANALYZE")
    try:
        row.set_activity_detail("x")
        row.toggle_expanded()
        QTest.qWait(260)
        height = row.height()
        assert 72 <= height <= 240
        assert height != 125, "the old hard-coded expanded height must be gone"
        # Long details are reachable through the embedded browser's own scroll.
        row.set_activity_detail("x " * 800)
        row._render_details()
        assert row._details_text.verticalScrollBar() is not None
    finally:
        row.deleteLater()
