"""Chat log auto-scroll — offscreen.

The old path scheduled a fresh timer on every append, and that timer scheduled
ANOTHER one — so a burst of inserts fired many competing "scroll to bottom"
calls (the reported jitter/jumping). The fix coalesces every request into a
single deferred scroll that runs after the layout has placed the newest row.

This file pins the reader-facing contract:

* a burst of appends queues exactly ONE pending scroll;
* a reader who scrolled up into older messages is NOT yanked to the bottom by
  an incoming J.A.R.V.I.S. message, and following resumes when they return
  near the bottom — or immediately for their OWN message;
* opening/restoring a conversation lands exactly on the newest message with an
  immediate jump (no animation) and re-settles if the layout keeps changing;
* resizing keeps a reader at the bottom pinned, and leaves a reader who is up
  in history exactly where they were;
* a finished entrance reveal re-pins the newest row into view.

Positioning runs in reduced-motion mode (``smooth_animations`` OFF) so the
assertions test the follow-state machine and the landing target, not Qt's
animation clock — the animation is a presentation detail, covered separately.
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

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_scroll_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


@pytest.fixture(autouse=True)
def _instant_scroll(monkeypatch):
    """Reduced motion: position immediately instead of gliding.

    Keeps every assertion deterministic (and fast) — the glide itself is not
    what these tests are about.
    """
    monkeypatch.setattr(ui, "_read_full_config",
                        lambda *a, **k: {"features": {"smooth_animations": False}})
    yield


def _pump(qapp, ms: int) -> None:
    """Run the event loop for *ms* so deferred timers can fire."""
    deadline = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def _wheel(log, delta_y: int, steps: int = 6) -> None:
    """Send real wheel gestures so the log registers genuine user scrolling.

    The scroll bar is deliberately hidden (``ScrollBarAlwaysOff``), so the wheel
    is exactly how a person moves this log; the follow-state is keyed on real
    input rather than on programmatic value changes.
    """
    for _ in range(steps):
        event = QWheelEvent(
            QPointF(20.0, 20.0), QPointF(20.0, 20.0),
            QPoint(0, 0), QPoint(0, int(delta_y)),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(log.viewport(), event)
    qapp = QApplication.instance()
    if qapp is not None:
        qapp.processEvents()


def _log(qapp, w: int = 400, h: int = 220) -> "ui._RoundedChatLog":
    log = ui._RoundedChatLog()
    log.show()
    log.resize(w, h)
    qapp.processEvents()
    return log


def _fill(log, count: int, prefix: str = 'row') -> None:
    """Fill with restored-style rows: real height now, no entrance animation."""
    for i in range(count):
        log.append_message('J.A.R.V.I.S.', f'{prefix} {i} ' + 'y' * 80, muted=True)


def test_burst_of_appends_coalesces_to_one_scroll(qapp):
    log = _log(qapp, 400, 260)

    fired = []
    real = ui._RoundedChatLog._scroll_bottom

    def spy(self):
        fired.append(1)
        return real(self)

    ui._RoundedChatLog._scroll_bottom = spy
    try:
        for i in range(6):
            log.append_message('J.A.R.V.I.S.', f'line {i} ' + 'x' * 60, muted=True)
        assert log._scroll_pending is True
        assert len(fired) == 0, "scroll must be deferred, not run synchronously"

        qapp.processEvents()
        assert len(fired) == 1, f"expected one coalesced scroll, got {len(fired)}"
        assert log._scroll_pending is False
    finally:
        ui._RoundedChatLog._scroll_bottom = real


def test_view_rests_at_bottom_after_update(qapp):
    log = _log(qapp, 400, 200)
    _fill(log, 10)
    _pump(qapp, 120)
    bar = log.verticalScrollBar()
    assert bar.maximum() > 0, "the fixture must actually overflow (non-vacuous)"
    assert bar.value() == bar.maximum(), "the newest content is not visible"


def test_resize_does_not_force_scroll_while_reading(qapp):
    log = _log(qapp, 400, 200)
    _fill(log, 10)
    _pump(qapp, 120)
    _wheel(log, +120)
    bar = log.verticalScrollBar()
    assert log.is_following_bottom() is False, "the wheel up must stop following"
    before = bar.value()
    assert before < bar.maximum(), "precondition: the reader is up in history"

    log.resize(420, 300)
    _pump(qapp, 80)
    assert bar.value() == before, "resize forced a scroll, breaking manual reading"


def test_resize_re_pins_when_parked_at_bottom(qapp):
    log = _log(qapp, 400, 200)
    _fill(log, 10)
    _pump(qapp, 120)
    assert log.is_following_bottom() is True
    log.resize(360, 260)
    _pump(qapp, 120)
    bar = log.verticalScrollBar()
    assert bar.value() == bar.maximum(), \
        "a resize must keep a reader parked at the bottom pinned there"


def test_user_message_returns_view_to_bottom_after_scrolling_up(qapp):
    """Your own message always follows to the newest line, even from history."""
    log = _log(qapp, 400, 200)
    _fill(log, 12)
    _pump(qapp, 120)
    bar = log.verticalScrollBar()
    _wheel(log, +120)
    assert bar.value() < bar.maximum(), "precondition: reader is up in history"
    assert log.is_following_bottom() is False

    # A restored-style row has its final height immediately, so the forced
    # follow is measured against the real range (the animated-reveal re-pin is
    # covered by its own test below).
    log.append_message('You', 'Hi Jarvis', you=True, muted=True)
    _pump(qapp, 160)
    assert bar.value() == bar.maximum(), \
        "the user's own message must bring the chat log back to the bottom"


def test_assistant_message_does_not_yank_a_reader_in_history(qapp):
    """An incoming reply must never drag a reader out of older messages."""
    log = _log(qapp, 400, 200)
    _fill(log, 12)
    _pump(qapp, 120)
    bar = log.verticalScrollBar()
    _wheel(log, +120)
    before = bar.value()
    assert before < bar.maximum()

    log.append_message('J.A.R.V.I.S.', 'A long incoming reply ' + 'z' * 160, muted=True)
    _pump(qapp, 160)
    assert bar.value() == before, \
        "an incoming message yanked a reader who was reading older messages"


def test_returning_near_bottom_resumes_following(qapp):
    log = _log(qapp, 400, 200)
    _fill(log, 12)
    _pump(qapp, 120)
    _wheel(log, +120)
    assert log.is_following_bottom() is False

    # Return near the bottom (within the follow threshold) with a wheel down.
    _wheel(log, -120, steps=30)
    assert log.is_following_bottom() is True, \
        "returning near the bottom must re-arm automatic following"

    log.append_message('J.A.R.V.I.S.', 'Now I may scroll you again.', muted=True)
    _pump(qapp, 160)
    bar = log.verticalScrollBar()
    assert bar.value() == bar.maximum(), \
        "following must resume after the reader returns to the bottom"


def test_restore_lands_on_newest_message_immediately(qapp):
    """Opening a conversation jumps to the latest message with no animation."""
    log = _log(qapp, 400, 200)
    _fill(log, 40, prefix='history')
    _pump(qapp, 120)
    assert log.verticalScrollBar().maximum() > 0
    log.verticalScrollBar().setValue(0)      # pretend the rebuild started at the top

    log.begin_settle_follow()
    # The jump itself is immediate — no animation tour from the top.
    assert log.verticalScrollBar().value() == log.verticalScrollBar().maximum()
    _pump(qapp, 260)
    bar = log.verticalScrollBar()
    assert bar.value() == bar.maximum(), \
        "a restored conversation must open on its newest message"
    assert log._settle_timer.isActive() is False, \
        "the restore settle loop must stop when the layout has settled"


def test_settle_loop_re_pins_a_late_height_change(qapp):
    """A restored log re-pins when rows keep changing height after the jump."""
    log = _log(qapp, 400, 200)
    _fill(log, 20, prefix='history')
    _pump(qapp, 120)

    log.begin_settle_follow(max_ticks=25, interval_ms=20)
    assert log._settle_timer.isActive() is True
    # A late layout change (as real font/word-wrap settling produces).
    log.append_message('J.A.R.V.I.S.', 'late row ' + 'l' * 140, muted=True)
    log._page.updateGeometry()
    _pump(qapp, 160)
    bar = log.verticalScrollBar()
    assert bar.value() == bar.maximum(), \
        "the settle loop must follow a late layout change to the bottom"
    _pump(qapp, 300)
    assert log._settle_timer.isActive() is False


def test_entrance_reveal_completion_re_pins(qapp):
    """When a row's reveal finishes, the log re-pins it into view.

    The reveal itself is a Qt animation (its clock is a presentation detail);
    what this pins is the contract that its completion re-requests the scroll,
    which is what brings a message that grew after the first scroll into view.
    """
    log = _log(qapp, 360, 160)
    _fill(log, 3)
    _pump(qapp, 120)

    log.append_message('J.A.R.V.I.S.', 'A tall revealed reply. ' + 'x' * 400)
    _pump(qapp, 60)                     # the deferred reveal start has run
    row = log._rows[-1]
    # Simulate the reveal's final state (its _done handler) and completion hook.
    row.setMaximumHeight(16777215)
    row.setGraphicsEffect(None)
    row.updateGeometry()
    log._on_row_revealed()
    _pump(qapp, 160)
    # The final layout pass changes the range by a pixel or two; Qt emits
    # rangeChanged for that, which is the hook that re-pins to the new bottom
    # (driven here directly, since the animation clock is a presentation
    # detail of the environment).
    log._on_range_changed(0, log.verticalScrollBar().maximum())
    _pump(qapp, 160)
    bar = log.verticalScrollBar()
    assert bar.value() == bar.maximum(), \
        "the newest row was not re-pinned after its reveal completed"


def test_smooth_scroll_is_chosen_when_animations_are_on(monkeypatch):
    """The glide is used only when Smooth animations is ON (reduced-motion)."""
    monkeypatch.setattr(ui, "_read_full_config",
                        lambda *a, **k: {"features": {"smooth_animations": True}})
    assert ui._RoundedChatLog._smooth_scroll_enabled() is True
    monkeypatch.setattr(ui, "_read_full_config",
                        lambda *a, **k: {"features": {"smooth_animations": False}})
    assert ui._RoundedChatLog._smooth_scroll_enabled() is False
