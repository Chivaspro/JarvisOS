"""Task progress panel + interactive question flow (embedded in the HUD).

Two widgets the main window places in a floating "TASK PROGRESS" panel:

* :class:`TaskProgressPane` — the live checklist for every task. It renders the
  REAL registry snapshots: task selector, state, elapsed time, current step,
  per-step outcomes, errors, decisions and artifacts, plus REPORT / CANCEL.
  Hiding the panel loses nothing: the registry is the source of truth and the
  pane re-renders from it whenever it is shown.

* :class:`QuestionFlowView` — one clarification question: options with the
  computed recommendation (and its reason), a custom answer, single/multi
  select, Back / Skip / Confirm navigation and the summary step. Every action
  is dispatched to ``brain.task_questions`` (the same service the dispatcher
  gate blocks on), so the on-screen panel is authoritative.

This module imports neither ``ui.py`` nor ``main.py`` (the existing
``ui_mcp_panel``/``ui_vault_panel`` convention), so it can never create an
import cycle; the HUD passes in the small provider callbacks it needs.
"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSizePolicy, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
    QStyle, QStyleOptionButton,
)

from ui_theme import (
    ACCENT, ACCENT_TEXT, AMBER, BG, BORDER, BORDER_SOFT, CARD, CARD_HI, FIELD_BG,
    FONT_HEAD, FONT_MONO, FONT_SMALL, FONT_UI, GREEN, RADIUS_MD, RADIUS_SM,
    RED, TEXT, TEXT_BRIGHT, TEXT_DIM, TEXT_MID, rgba, scrollbar_css,
)

# ui_theme is the single source of truth for the running HUD's colors and fonts.
# Task-specific UI states choose semantic tokens rather than maintaining a second palette.

__all__ = ["TaskProgressPane", "QuestionFlowView"]

_STEP_GLYPH = {
    "completed": ("✔", GREEN),
    "failed": ("✖", RED),
    "skipped": ("–", TEXT_DIM),
    "running": ("◐", ACCENT),
    "waiting": ("?", AMBER),
    "pending": ("○", TEXT_DIM),
}
_STATE_COLOR = {
    "CREATED": TEXT_MID,
    "INITIALIZING": ACCENT,
    "RUNNING": ACCENT,
    "VALIDATING": AMBER,
    "COMPLETED": GREEN,
    "FAILED": RED,
    "CANCELLED": TEXT_DIM,
}


def _service():
    """The live question service (None when the brain module is unavailable)."""
    try:
        from brain.task_questions import get_question_service

        return get_question_service()
    except Exception:
        return None


class _QuestionCheckBox(QCheckBox):
    """Question option with an explicit high-contrast, painted indicator.

    Native Qt checkbox indicators can inherit platform styling that makes the
    check mark blend into the light panel. Paint the indicator over the native
    checkbox after the normal label/layout has been drawn, so interaction and
    keyboard accessibility stay native while the checked state is always clear.
    """

    _CHECKED = QColor(ACCENT)
    _CHECKED_BORDER = QColor(ACCENT_TEXT)
    _UNCHECKED = QColor(BG)
    _UNCHECKED_HOVER = QColor(CARD_HI)
    _BORDER = QColor(BORDER)
    _CHECK = QColor(TEXT_BRIGHT)

    def hitButton(self, pos) -> bool:  # noqa: N802 - Qt virtual method name
        """Make the complete option row clickable across Qt/platform styles."""
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        if not indicator.isValid() or indicator.isEmpty():
            return

        # Cover the platform's indicator with a consistent blue surface and
        # draw our own tick; the label remains painted by Qt as usual.
        rect = QRectF(indicator).adjusted(1.0, 1.0, -1.0, -1.0)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self._CHECKED_BORDER if self.isChecked() else self._BORDER)
        if self.isChecked():
            painter.setBrush(self._CHECKED)
        elif self.underMouse():
            painter.setBrush(self._UNCHECKED_HOVER)
        else:
            painter.setBrush(self._UNCHECKED)
        painter.drawRoundedRect(rect, 3.0, 3.0)

        if self.isChecked():
            x, y = float(rect.x()), float(rect.y())
            w, h = float(rect.width()), float(rect.height())
            tick = QPainterPath()
            tick.moveTo(x + w * 0.20, y + h * 0.52)
            tick.lineTo(x + w * 0.42, y + h * 0.73)
            tick.lineTo(x + w * 0.81, y + h * 0.27)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(self._CHECK)
            pen = painter.pen()
            pen.setWidthF(2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(tick)
        painter.end()


class _CornerLineFrame(QFrame):
    """Dark Activity-style surface with soft fill and four open corner strokes."""

    def __init__(self, parent=None, *, edge=None, fill=None):
        super().__init__(parent)
        self._edge = QColor(edge or ACCENT)
        self._fill = QColor(fill or QColor(1, 15, 25, 135))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background:transparent;border:none;")

    def set_edge_color(self, color) -> None:
        self._edge = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        if rect.width() > 0 and rect.height() > 0:
            p.fillRect(rect, self._fill)
            edge = QColor(self._edge)
            edge.setAlpha(min(210, max(90, edge.alpha())))
            p.setPen(QPen(edge, 1.15, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            arm = min(12.0, max(6.0, min(rect.width(), rect.height()) * 0.16))
            l, t, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
            for x, y, dx, dy in ((l, t, 1, 1), (r, t, -1, 1),
                                  (l, b, 1, -1), (r, b, -1, -1)):
                p.drawLine(QPointF(x, y), QPointF(x + dx * arm, y))
                p.drawLine(QPointF(x, y), QPointF(x, y + dy * arm))
        p.end()


class _CornerLineButton(QPushButton):
    """Button that uses the HUD's open-corner treatment, never a rounded box."""

    def __init__(self, text: str, *, accent: bool = False, danger: bool = False,
                 compact: bool = False):
        super().__init__(text)
        self._accent = bool(accent)
        self._danger = bool(danger)
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        pad = "4px 10px" if compact else "6px 14px"
        self.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;padding:{pad};"
            f"font:700 8pt {FONT_UI};}}"
        )

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        r = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        if r.width() <= 0 or r.height() <= 0:
            p.end()
            return
        active = self._hover or self.isDown() or self.hasFocus()
        if self.isDown():
            p.fillRect(r, QColor(0, 92, 124, 105))
        elif self._hover:
            p.fillRect(r, QColor(0, 72, 98, 62))
        elif self._accent:
            p.fillRect(r, QColor(0, 58, 80, 28))

        if not self.isEnabled():
            ink = QColor(TEXT_DIM)
            edge = QColor(BORDER_SOFT)
        elif self._danger:
            ink = QColor(RED if active else "#E77B8B")
            edge = QColor(RED)
        elif self._accent:
            ink = QColor(ACCENT_TEXT if active else ACCENT)
            edge = QColor(ACCENT)
        else:
            ink = QColor(TEXT_BRIGHT if active else TEXT_MID)
            edge = QColor(ACCENT if active else BORDER)
        edge.setAlpha(220 if active or self._accent or self._danger else 145)
        p.setPen(QPen(edge, 1.15, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        arm = min(8.5, max(5.0, min(r.width(), r.height()) * 0.23))
        l, t, right, bottom = r.left(), r.top(), r.right(), r.bottom()
        for x, y, dx, dy in ((l, t, 1, 1), (right, t, -1, 1),
                              (l, bottom, 1, -1), (right, bottom, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * arm, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * arm))
        font = QFont(FONT_UI.strip("'\""), 8, QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(ink)
        p.drawText(r.adjusted(4, 0, -4, 0),
                   int(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter),
                   self.text())
        p.end()


def _btn(text: str, *, accent: bool = False, danger: bool = False,
         compact: bool = False) -> QPushButton:
    """Activity-matched angular button; same size hints and callbacks as before."""
    return _CornerLineButton(text, accent=accent, danger=danger, compact=compact)


# ── question flow ─────────────────────────────────────────────────────────────

class QuestionFlowView(_CornerLineFrame):
    """One question (or the summary) of an open clarification flow."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, edge=ACCENT, fill=QColor(1, 15, 25, 155))
        self._task_id = ""
        self._snapshot: dict = {}
        self._selected: list[str] = []
        self._checks: dict[str, QCheckBox] = {}
        self._build()

    def _build(self) -> None:
        self.setStyleSheet(
            "QFrame{background:transparent;border:none;}"
            "QLabel{background:transparent;border:none;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 9, 10, 9)
        lay.setSpacing(6)

        self._header = QLabel("CLARIFICATION NEEDED")
        self._header.setStyleSheet(
            f"color:{ACCENT};font:800 7pt {FONT_HEAD};letter-spacing:1px;")
        lay.addWidget(self._header)

        self._title = QLabel("")
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color:{TEXT_BRIGHT};font:700 9.5pt {FONT_UI};")
        lay.addWidget(self._title)

        self._explain = QLabel("")
        self._explain.setWordWrap(True)
        self._explain.setStyleSheet(f"color:{TEXT_MID};font:400 8pt {FONT_SMALL};")
        lay.addWidget(self._explain)

        self._options_box = QVBoxLayout()
        self._options_box.setSpacing(3)
        lay.addLayout(self._options_box)

        self._custom = QLineEdit()
        self._custom.setPlaceholderText("Or type your own answer…")
        self._custom.textChanged.connect(self._sync_selected)
        self._custom.setStyleSheet(
            f"QLineEdit{{background:rgba(1,15,25,150);color:{TEXT};"
            f"border:1px solid {BORDER};border-radius:0px;"
            f"padding:5px 7px;font:400 8.5pt {FONT_UI};}}"
            f"QLineEdit:focus{{border:1px solid {ACCENT};}}")
        lay.addWidget(self._custom)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(
            f"color:{TEXT};font:400 8.5pt {FONT_MONO};background:transparent;")
        lay.addWidget(self._summary)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color:{AMBER};font:700 7.5pt {FONT_SMALL};")
        lay.addWidget(self._error)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._back = _btn("◂ BACK", compact=True)
        self._back.clicked.connect(self._on_back)
        self._skip = _btn("SKIP", compact=True)
        self._skip.clicked.connect(self._on_skip)
        self._next = _btn("NEXT ▸", accent=True, compact=True)
        self._next.clicked.connect(self._on_next)
        self._confirm = _btn("CONFIRM & RUN", accent=True)
        self._confirm.clicked.connect(self._on_confirm)
        self._cancel = _btn("CANCEL", danger=True, compact=True)
        self._cancel.clicked.connect(self._on_cancel)
        row.addWidget(self._back)
        row.addWidget(self._skip)
        row.addStretch(1)
        row.addWidget(self._cancel)
        row.addWidget(self._next)
        row.addWidget(self._confirm)
        lay.addLayout(row)

    # ── rendering ─────────────────────────────────────────────────────────────
    def set_snapshot(self, snapshot: dict | None) -> None:
        """Render a question-flow snapshot (empty hides the view)."""
        snapshot = dict(snapshot or {})
        self._task_id = str(snapshot.get("task_id") or "")
        self._snapshot = snapshot
        state = str(snapshot.get("state") or "")
        if not snapshot or state in ("CONFIRMED", "CANCELLED", ""):
            self.setVisible(False)
            return
        self.setVisible(True)
        self._error.setText("")
        question = snapshot.get("question") or {}
        if state == "SUMMARY":
            self._render_summary(snapshot)
            return
        self._render_question(question)

    def _clear_options(self) -> None:
        while self._options_box.count():
            item = self._options_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Detach immediately: deleteLater() alone leaves the widget
                # parented (and findable) until the event loop drains.
                widget.setParent(None)
                widget.deleteLater()
        self._checks = {}
        self._selected = []

    def _render_question(self, question: dict) -> None:
        self._summary.setVisible(False)
        self._custom.setVisible(True)
        self._confirm.setVisible(False)
        self._next.setVisible(True)
        self._title.setText(str(question.get("title") or "Clarification"))
        self._explain.setText(str(question.get("explanation") or ""))
        multi = bool(question.get("multi_select"))
        recommended = str(question.get("recommended_option") or "")
        reason = str(question.get("recommended_reason") or "")
        question_id = str(question.get("id") or "")
        answered_ids = self._answers_for(question_id)
        if not multi and answered_ids:
            answered_ids = answered_ids[:1]
        self._clear_options()
        for option in question.get("options") or []:
            oid = str(option.get("id") or "")
            label = str(option.get("label") or oid)
            description = str(option.get("description") or "")
            text = label
            if description:
                text = f"{label} — {description}"
            if oid == recommended and reason:
                text = f"★ {text}  ·  recommended: {reason}"
            box = _QuestionCheckBox(text)
            box.setStyleSheet(
                f"QCheckBox{{color:{TEXT};font:500 8.5pt {FONT_UI};"
                f"padding:4px 3px;spacing:8px;background:transparent;}}"
                f"QCheckBox::indicator{{width:18px;height:18px;}}"
                f"QCheckBox:disabled{{color:{TEXT_DIM};}}")
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            box.setMouseTracking(True)
            box.setMinimumWidth(0)
            box.setMinimumHeight(34)
            box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            box.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
            box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            box.setTristate(False)
            box.setAccessibleName(label)
            box.setAccessibleDescription(description or label)
            box.setChecked(oid in answered_ids)
            # ``toggled`` fires only for a real boolean change.  The callback
            # reads every checked row, so restored answers and fresh clicks use
            # the same source of truth.
            box.toggled.connect(
                lambda _checked, _o=oid, _m=multi: self._on_option(_o, _m))
            self._options_box.addWidget(box)
            self._checks[oid] = box
        self._custom.setVisible(bool(question.get("allow_custom")))
        if not question.get("allow_custom"):
            self._custom.clear()
        self._back.setEnabled(bool(self._snapshot.get("can_go_back")))
        self._skip.setVisible(not bool(question.get("required")))
        # A hinted recommendation is never a selection — the user must choose.
        self._sync_selected()

    def _answers_for(self, question_id: str) -> list[str]:
        detail = self._snapshot.get("answered_detail") or {}
        value = detail.get(question_id) or {}
        return [str(o) for o in (value.get("option_ids") or [])]

    def _render_summary(self, snapshot: dict) -> None:
        self._clear_options()
        self._title.setText("Confirm the plan")
        self._explain.setText(
            "These decisions will shape the work. Confirm to continue, or go back "
            "to change an answer.")
        self._custom.setVisible(False)
        self._summary.setVisible(True)
        self._summary.setText(str(snapshot.get("summary") or "(nothing selected)"))
        self._back.setEnabled(True)
        self._skip.setVisible(False)
        self._next.setVisible(False)
        self._confirm.setVisible(True)

    def _on_option(self, option_id: str, multi: bool) -> None:
        box = self._checks.get(option_id)
        if box is None:
            return
        if not multi and box.isChecked():
            # Single-select questions may keep exactly one checked option.
            for oid, other in self._checks.items():
                if oid != option_id and other.isChecked():
                    other.blockSignals(True)
                    other.setChecked(False)
                    other.blockSignals(False)
        self._sync_selected()

    def _sync_selected(self, *_args) -> None:
        """Rebuild the submitted answer from the visible, actual control state."""
        checked = [oid for oid, box in self._checks.items() if box.isChecked()]
        question = self._snapshot.get("question") or {}
        if not bool(question.get("multi_select")) and len(checked) > 1:
            checked = checked[:1]
            keep = checked[0]
            for oid, box in self._checks.items():
                if oid != keep and box.isChecked():
                    box.blockSignals(True)
                    box.setChecked(False)
                    box.blockSignals(False)
        self._selected = checked
        custom_allowed = bool(question.get("allow_custom"))
        has_custom = custom_allowed and bool(self._custom.text().strip())
        has_answer = bool(self._selected) or has_custom
        if hasattr(self, "_next"):
            self._next.setEnabled(has_answer or not bool(question.get("required")))

    # ── actions (dispatched to the brain question service) ────────────────────
    def _on_next(self) -> None:
        service = _service()
        if service is None or not self._task_id:
            return
        question_id = str((self._snapshot.get("question") or {}).get("id") or "")
        error = service.resolve_from_panel(
            self._task_id, question_id, list(self._selected),
            custom=self._custom.text().strip())
        if error:
            self._error.setText(error)
        else:
            self._custom.clear()

    def _on_back(self) -> None:
        service = _service()
        if service is not None and self._task_id:
            self._error.setText(service.back(self._task_id) or "")

    def _on_skip(self) -> None:
        service = _service()
        if service is not None and self._task_id:
            self._error.setText(service.skip(self._task_id) or "")

    def _on_confirm(self) -> None:
        service = _service()
        if service is not None and self._task_id:
            message = service.confirm(self._task_id)
            if message and not message.startswith("Confirmed"):
                self._error.setText(message)

    def _on_cancel(self) -> None:
        service = _service()
        if service is not None and self._task_id:
            service.cancel(self._task_id, "cancelled from the task panel")


# ── execution approval card ───────────────────────────────────────────────────

_KIND_LABEL = {
    "coding": ("CODING", ACCENT),
    "file_change": ("FILE CHANGE", AMBER),
    "desktop": ("DESKTOP CONTROL", AMBER),
    "plugin": ("PLUGIN ACTION", ACCENT),
    "automation": ("AUTOMATION", AMBER),
    "action": ("ACTION", ACCENT),
}
_STATUS_COLOR = {
    "pending": AMBER,
    "approved": GREEN,
    "declined": RED,
    "expired": TEXT_DIM,
    "cancelled": TEXT_DIM,
}


def _approval_service():
    try:
        from security.approval import get_approval_service
        return get_approval_service()
    except Exception:
        return None


class ApprovalCard(_CornerLineFrame):
    """One real execution approval: what will happen, where, and the verdict.

    The card never decides anything itself — it reports the user's press to the
    central approval service (through the pane), so the same decision unblocks
    the waiting tool regardless of whether the user clicked here or in chat.
    """

    decided = pyqtSignal(str, bool, bool)  # request_id, accepted, auto_approve

    def __init__(self, parent=None) -> None:
        super().__init__(parent, edge=AMBER, fill=QColor(18, 12, 2, 165))
        self._request: dict = {}
        self._request_id = ""
        self._kind = "action"
        self._status = ""
        self._build()

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.setStyleSheet("QFrame{background:transparent;border:none;}"
                           "QLabel{background:transparent;border:none;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 9, 11, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self._badge = QLabel("APPROVAL REQUIRED")
        self._badge.setStyleSheet(
            f"color:{AMBER};font:800 7pt {FONT_HEAD};letter-spacing:1px;")
        head.addWidget(self._badge)
        head.addStretch(1)
        self._status_lbl = QLabel("PENDING")
        self._status_lbl.setStyleSheet(
            f"color:{AMBER};font:800 7pt {FONT_HEAD};letter-spacing:1px;")
        head.addWidget(self._status_lbl)
        lay.addLayout(head)

        self._title = QLabel("")
        self._title.setWordWrap(True)
        self._title.setStyleSheet(
            f"color:{TEXT_BRIGHT};font:700 10pt {FONT_UI};")
        lay.addWidget(self._title)

        self._question = QLabel("")
        self._question.setWordWrap(True)
        self._question.setStyleSheet(f"color:{TEXT};font:500 8.5pt {FONT_UI};")
        lay.addWidget(self._question)

        self._description = QLabel("")
        self._description.setWordWrap(True)
        self._description.setStyleSheet(
            f"color:{TEXT_MID};font:400 8pt {FONT_SMALL};")
        lay.addWidget(self._description)

        # Affected files / concrete changes.  The list is scrollable so a large
        # diff summary can never push the Approve/Decline buttons off the pane.
        self._changes_scroll = QScrollArea()
        self._changes_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._changes_scroll.setWidgetResizable(True)
        self._changes_scroll.setMaximumHeight(132)
        self._changes_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}"
            + scrollbar_css(4))
        self._changes_host = QWidget()
        self._changes_host.setStyleSheet("QWidget{background:transparent;}")
        self._changes_lay = QVBoxLayout(self._changes_host)
        self._changes_lay.setContentsMargins(0, 0, 2, 0)
        self._changes_lay.setSpacing(4)
        self._changes_scroll.setWidget(self._changes_host)
        self._changes_scroll.setVisible(False)
        lay.addWidget(self._changes_scroll)

        self._reason = QLabel("")
        self._reason.setWordWrap(True)
        self._reason.setStyleSheet(f"color:{TEXT_DIM};font:400 7.5pt {FONT_SMALL};")
        self._reason.setVisible(False)
        lay.addWidget(self._reason)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 2, 0, 0)
        foot.setSpacing(8)
        self._auto = QCheckBox("AUTO-APPROVE")
        self._auto.setToolTip(
            "Skip this card for future actions of the same kind. This does not "
            "approve the current action.")
        self._auto.setStyleSheet(
            f"QCheckBox{{color:{TEXT_MID};font:700 7pt {FONT_SMALL};"
            "letter-spacing:0.6px;spacing:7px;background:transparent;}"
            "QCheckBox::indicator{width:15px;height:15px;}"
            f"QCheckBox:checked{{color:{AMBER};}}")
        self._auto.toggled.connect(self._on_auto_toggled)
        foot.addWidget(self._auto)
        foot.addStretch(1)
        self._approve = _btn("APPROVE", accent=True, compact=True)
        self._approve.setObjectName("approveActionButton")
        self._approve.clicked.connect(lambda: self._finish(True))
        self._decline = _btn("DECLINE", danger=True, compact=True)
        self._decline.setObjectName("declineActionButton")
        self._decline.clicked.connect(lambda: self._finish(False))
        foot.addWidget(self._decline)
        foot.addWidget(self._approve)
        lay.addLayout(foot)

    # ── rendering ────────────────────────────────────────────────────────────
    def _clear_changes(self) -> None:
        while self._changes_lay.count():
            item = self._changes_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def set_request(self, request: dict) -> None:
        request = dict(request or {})
        self._request = request
        self._request_id = str(request.get("request_id") or "")
        self._kind = str(request.get("kind") or "action").lower()
        label, color = _KIND_LABEL.get(self._kind, _KIND_LABEL["action"])
        self._badge.setText(f"APPROVAL REQUIRED · {label}")
        self._badge.setStyleSheet(
            f"color:{color};font:800 7pt {FONT_HEAD};letter-spacing:1px;")
        self.set_edge_color(color)
        self._title.setText(str(request.get("title") or "Approval required"))
        question = str(request.get("question") or "").strip()
        self._question.setText(question if question and question != self._title.text() else "")
        self._question.setVisible(bool(self._question.text()))
        description = str(request.get("description") or "").strip()
        self._description.setText(description)
        self._description.setVisible(bool(description))

        self._clear_changes()
        targets = [str(t) for t in (request.get("targets") or []) if str(t).strip()]
        changes = request.get("changes") or {}
        for path in targets:
            row = QLabel(f"▸ {path}")
            row.setWordWrap(True)
            row.setStyleSheet(
                f"color:{TEXT};font:400 7.5pt {FONT_MONO};")
            self._changes_lay.addWidget(row)
            summary = str(changes.get(path) or "").strip()
            if summary:
                detail = QLabel(f"    {summary}")
                detail.setWordWrap(True)
                detail.setStyleSheet(
                    f"color:{TEXT_DIM};font:400 7.5pt {FONT_SMALL};")
                self._changes_lay.addWidget(detail)
        for path, summary in changes.items():
            if path in targets:
                continue
            row = QLabel(f"▸ {path}\n    {summary}")
            row.setWordWrap(True)
            row.setStyleSheet(f"color:{TEXT};font:400 7.5pt {FONT_MONO};")
            self._changes_lay.addWidget(row)
        self._changes_scroll.setVisible(bool(targets or changes))

        try:
            self._auto.blockSignals(True)
            self._auto.setChecked(bool(request.get("auto_approve")))
            self._auto.blockSignals(False)
        except Exception:
            pass
        self._reason.setText("")
        self._reason.setVisible(False)
        self.set_status("pending")
        self.setVisible(True)

    def set_status(self, status: str, reason: str = "") -> None:
        status = str(status or "pending").lower()
        self._status = status
        color = _STATUS_COLOR.get(status, TEXT_DIM)
        self._status_lbl.setText(status.upper())
        self._status_lbl.setStyleSheet(
            f"color:{color};font:800 7pt {FONT_HEAD};letter-spacing:1px;")
        terminal = status not in ("", "pending")
        self._approve.setEnabled(not terminal)
        self._decline.setEnabled(not terminal)
        self._approve.setVisible(status in ("", "pending"))
        self._decline.setVisible(status in ("", "pending"))
        if reason:
            self._reason.setText(reason)
            self._reason.setVisible(True)
        if terminal:
            self.set_edge_color(color)

    def status(self) -> str:
        return self._status

    def request_id(self) -> str:
        return self._request_id

    # ── interaction ──────────────────────────────────────────────────────────
    def _finish(self, accepted: bool) -> None:
        if not self._request_id or self._status not in ("", "pending"):
            return
        # Disable immediately: a double-click must never emit two decisions.
        self.set_status("approved" if accepted else "declined",
                        "Recording your decision…")
        self.decided.emit(self._request_id, bool(accepted), self._auto.isChecked())

    def _on_auto_toggled(self, checked: bool) -> None:
        service = _approval_service()
        if service is None:
            return
        try:
            service.set_auto_approve(self._kind, bool(checked))
        except Exception:
            pass

    def refresh_auto_flag(self) -> None:
        service = _approval_service()
        if service is None:
            return
        try:
            self._auto.blockSignals(True)
            self._auto.setChecked(service.auto_approve_enabled(self._kind))
            self._auto.blockSignals(False)
        except Exception:
            pass


# ── task progress pane ────────────────────────────────────────────────────────

class TaskProgressPane(QWidget):
    """Compact, glass-like checklist driven by live task-registry snapshots."""

    preferredSizeChanged = pyqtSignal()
    _TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "ERROR"}
    _SPINNER = ("◴", "◷", "◶", "◵")

    def __init__(self, *, list_provider: Callable[[], list[dict]] | None = None,
                 report_provider: Callable[[str], str] | None = None,
                 on_cancel: Callable[[str], None] | None = None,
                 on_report: Callable[[str, str], None] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._list_provider = list_provider
        self._report_provider = report_provider
        self._on_cancel = on_cancel
        self._on_report = on_report
        self._snapshots: dict[str, dict] = {}
        self._order: list[str] = []
        self._selected = ""
        self._spinner_index = 0
        self._running_mark: QLabel | None = None
        self._rendering = False
        self._approval_queue: list[dict] = []
        self._build()
        self._ticker = QTimer(self)
        self._ticker.setInterval(250)
        self._ticker.timeout.connect(self._tick)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._ticker.isActive():
            self._ticker.start()

    def hideEvent(self, event) -> None:
        self._ticker.stop()
        super().hideEvent(event)

    # ── construction ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.setObjectName("TeardownChecklistPane")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        # Use a real tinted pane fallback instead of allowing platform-native
        # QWidget/viewport defaults to leak through as white.
        pane_palette = self.palette()
        pane_palette.setColor(QPalette.ColorRole.Window, QColor(BG))
        self.setPalette(pane_palette)
        self.setStyleSheet(
            "QWidget#TeardownChecklistPane{background:transparent;border:none;}"
            "QLabel{background:transparent;border:none;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(9)

        # Kept as a hidden compatibility/control model. The interface follows the
        # active task automatically, so a task picker would only add clutter.
        self._selector = QComboBox(self)
        self._selector.setObjectName("taskSelectorModel")
        self._selector.currentIndexChanged.connect(self._on_select_index)
        self._selector.hide()

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self._status = QLabel("WAITING")
        self._status.setObjectName("taskStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setMinimumHeight(24)
        self._status.setStyleSheet(
            f"QLabel{{color:{ACCENT};background:transparent;"
            "border:none;border-radius:0;padding:3px 2px;"
            f"font:700 7pt '{FONT_SMALL}';letter-spacing:1px;}}"
        )
        toolbar.addWidget(self._status, 0, Qt.AlignmentFlag.AlignLeft)
        toolbar.addStretch(1)
        self._elapsed = QLabel("00:00")
        self._elapsed.setObjectName("taskElapsed")
        self._elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._elapsed.setStyleSheet(
            f"QLabel{{color:{TEXT_DIM};font:600 10pt '{FONT_MONO}';padding:2px 4px;}}"
        )
        self._elapsed.setToolTip("Elapsed teardown time")
        toolbar.addWidget(self._elapsed)
        self._report_btn = _btn("REPORT", compact=True)
        self._report_btn.setToolTip("Open the detailed task report")
        self._report_btn.clicked.connect(self._on_report_clicked)
        toolbar.addWidget(self._report_btn)
        self._cancel_btn = _btn("Cancel", compact=True)
        self._cancel_btn.setObjectName("cancelTaskButton")
        self._cancel_btn.setToolTip("Stop the current teardown task")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        toolbar.addWidget(self._cancel_btn)
        lay.addLayout(toolbar)

        self._current = QLabel("Ready for a task")
        self._current.setObjectName("taskTitle")
        self._current.setWordWrap(True)
        self._current.setStyleSheet(
            f"QLabel{{color:{TEXT_BRIGHT};font:700 12pt '{FONT_HEAD}';padding:1px 2px;}}"
        )
        self._current.setMinimumWidth(0)
        self._current.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lay.addWidget(self._current)

        self._question = QuestionFlowView()
        self._question.setVisible(False)
        lay.addWidget(self._question)

        self._approval = ApprovalCard()
        self._approval.setVisible(False)
        self._approval.decided.connect(self._on_approval_decided)
        lay.addWidget(self._approval)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("taskChecklistScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._scroll.setStyleSheet(
            "QScrollArea#taskChecklistScroll{background:transparent;border:none;}"
            "QScrollArea#taskChecklistScroll QWidget#qt_scrollarea_viewport{background:transparent;border:none;}"
            + scrollbar_css(5)
        )
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.viewport().setStyleSheet("background:transparent;border:none;")
        self._steps_host = QWidget()
        self._steps_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._steps_host.setStyleSheet("QWidget{background:transparent;border:none;}")
        self._steps_lay = QVBoxLayout(self._steps_host)
        self._steps_lay.setContentsMargins(0, 1, 2, 1)
        self._steps_lay.setSpacing(5)
        self._empty_steps = QLabel("Waiting for concrete execution steps from the task runner…")
        self._empty_steps.setObjectName("taskChecklistEmptyState")
        self._empty_steps.setWordWrap(True)
        self._empty_steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_steps.setMinimumHeight(54)
        self._empty_steps.setStyleSheet(
            f"QLabel#taskChecklistEmptyState{{color:{TEXT_DIM};"
            f"background:{rgba(BG, 90)};border:none;"
            f"padding:12px;font:400 9pt '{FONT_MONO}';}}"
        )
        self._steps_lay.addWidget(self._empty_steps)
        self._steps_lay.addStretch(1)
        self._scroll.setWidget(self._steps_host)
        lay.addWidget(self._scroll, 1)

        self._report_card = _CornerLineFrame(
            self, edge=QColor(ACCENT), fill=QColor(1, 15, 25, 150))
        self._report_card.setObjectName("TaskReportCard")
        self._report_card.setStyleSheet(
            "QFrame#TaskReportCard{background:transparent;border:none;}"
            "QLabel{background:transparent;border:none;}"
        )
        self._report_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._report_card.setMinimumWidth(0)
        report_lay = QVBoxLayout(self._report_card)
        report_lay.setContentsMargins(11, 9, 11, 9)
        report_lay.setSpacing(4)
        self._report_title = QLabel("Teardown report")
        self._report_title.setStyleSheet(
            "QLabel{color:#5BDDF8;font:800 8pt 'Orbitron';letter-spacing:0.4px;}"
        )
        report_lay.addWidget(self._report_title)
        self._report_metrics = QLabel("")
        self._report_metrics.setWordWrap(True)
        self._report_metrics.setMinimumWidth(0)
        self._report_metrics.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._report_metrics.setStyleSheet(
            "QLabel{color:#C2EAF5;font:500 8.5pt 'Rajdhani';}"
        )
        report_lay.addWidget(self._report_metrics)
        self._report_errors = QLabel("")
        self._report_errors.setWordWrap(True)
        self._report_errors.setStyleSheet(
            "QLabel{color:#FF7E8B;font:400 8pt 'Rajdhani';}"
        )
        self._report_errors.setMinimumWidth(0)
        self._report_errors.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        report_lay.addWidget(self._report_errors)

        # Full report details stay inside the checklist instead of opening a
        # separate generic content window. The fixed viewport keeps long logs
        # readable without forcing the floating panel off-screen.
        self._report_detail = QTextBrowser(self._report_card)
        self._report_detail.setObjectName("TaskReportDetails")
        self._report_detail.setReadOnly(True)
        self._report_detail.setOpenExternalLinks(False)
        self._report_detail.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._report_detail.setMinimumHeight(126)
        self._report_detail.setMaximumHeight(230)
        self._report_detail.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._report_detail.setAutoFillBackground(False)
        self._report_detail.setFrameShape(QFrame.Shape.NoFrame)
        report_text_palette = self._report_detail.palette()
        report_text_palette.setColor(QPalette.ColorRole.Base, QColor("#071C2A"))
        report_text_palette.setColor(QPalette.ColorRole.Text, QColor("#DDF7FF"))
        report_text_palette.setColor(QPalette.ColorRole.Window, QColor("#071C2A"))
        self._report_detail.setPalette(report_text_palette)
        report_css = (
            "QTextBrowser#TaskReportDetails{"
            "background:rgba(1,15,25,115);color:#DDF7FF;"
            "border:none;padding:8px;"
            "selection-background-color:#0E5670;}"
            "QScrollBar:vertical{width:7px;background:#041522;margin:2px;border:none;}"
            "QScrollBar::handle:vertical{background:#2A849F;border-radius:3px;min-height:22px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        self._report_detail.setStyleSheet(report_css)
        # QTextBrowser paints its viewport separately; styling only the outer
        # frame leaves that child white on some Qt platform styles.
        self._report_detail.viewport().setAutoFillBackground(False)
        self._report_detail.viewport().setStyleSheet(
            "background:transparent;border:none;"
        )
        self._report_detail.document().setDefaultStyleSheet(
            "body { color: #DDF7FF; font-family: 'Rajdhani'; font-size: 10pt; }"
            "h1,h2,h3,h4 { color: #73E6FF; margin-top: 8px; margin-bottom: 4px; }"
            "p,li { color: #B9E8F5; }"
            "a { color: #65DFFB; text-decoration: underline; }"
            "pre,code { background-color: #0A2A3A; color: #C5F1FF; }"
            "blockquote { color: #82B7C8; border-left: 3px solid #2C8BA6; margin-left: 6px; padding-left: 8px; }"
            "table { border-collapse: collapse; } th,td { border: 1px solid #235568; padding: 4px 6px; }"
        )
        self._report_detail.hide()
        report_lay.addWidget(self._report_detail)
        self._report_card.hide()
        lay.addWidget(self._report_card)
        # Report card comes before the scrolling checklist so its summary is
        # visible immediately when the run finishes, even in a compact window.
        lay.removeWidget(self._report_card)
        lay.insertWidget(lay.indexOf(self._scroll), self._report_card, 0)

        self._detail = QLabel("")
        self._detail.setObjectName("taskDetails")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(
            "QLabel{color:#82B7C8;font:400 8pt 'Rajdhani';background:transparent;}"
        )
        self._detail.setSizePolicy(QSizePolicy.Policy.Preferred,
                                   QSizePolicy.Policy.Minimum)
        lay.addWidget(self._detail)

    # ── public API used by the HUD ────────────────────────────────────────────
    def apply_event(self, payload: dict) -> None:
        """Apply a registry event and follow the newest task being executed.

        The pane itself is persistent, but each user request has its own task
        record.  A fresh ``task_created`` event must therefore switch the pane
        to that new record even when the previously selected task is still
        marked RUNNING (for example, when the user asks to scan memory after
        opening the panel).  Waiting for the old task to become terminal was
        the reason the checklist appeared stuck on the previous request.
        """
        payload = dict(payload or {})
        task_id = str(payload.get("id") or "")
        if not task_id:
            return
        old_selected = self._selected
        event = str(payload.get("event") or "")
        self._remember(payload)

        if event == "task_created":
            # New request takes focus immediately; the same floating UI is
            # reused, but its checklist contents are replaced with this task.
            self._selected = task_id
        else:
            # Also recover gracefully if the create event was missed/delayed:
            # the most recently-created non-terminal task is authoritative.
            self._follow_latest_active()
            if not self._selected:
                self._selected = task_id

        self._advance_if_needed()
        if self._selected == task_id or self._selected != old_selected:
            self._render()

    def refresh(self) -> None:
        """Re-read records and make the newest active task the visible checklist."""
        changed = self._poll_snapshots()
        old_selected = self._selected
        self._follow_latest_active()
        if not self._selected and self._order:
            self._selected = self._order[-1]
        self._advance_if_needed()
        if changed or self._selected != old_selected or self._selected:
            self._render()

    def select_task(self, task_id: str) -> None:
        if task_id and str(task_id) in self._snapshots:
            self._selected = str(task_id)
        elif task_id:
            self._selected = str(task_id)
        self._render()

    def current_task_id(self) -> str:
        return self._selected

    def show_question(self, snapshot: dict) -> None:
        snapshot = dict(snapshot or {})
        self._question.set_snapshot(snapshot)
        self._question.updateGeometry()
        task_id = str(snapshot.get("task_id") or "")
        if task_id:
            self._selected = task_id
            self._render()

    # ── execution approvals ───────────────────────────────────────────────────
    def show_approval(self, request: dict) -> None:
        """Display one approval card; additional pending requests queue."""
        request = dict(request or {})
        request_id = str(request.get("request_id") or "")
        if not request_id:
            return
        current = self._approval.request_id()
        if (current and current != request_id
                and self._approval.status() in ("", "pending")):
            if not any(str(q.get("request_id") or "") == request_id
                       for q in self._approval_queue):
                self._approval_queue.append(request)
            return
        if current == request_id:
            self._approval.set_request(request)
        else:
            self._approval.set_request(request)
        self._approval.setVisible(True)
        self._approval.updateGeometry()
        self.preferredSizeChanged.emit()

    def update_approval(self, request_id: str, status: str,
                        reason: str = "") -> bool:
        """Reflect a decision made elsewhere (chat card / auto-expiry)."""
        request_id = str(request_id or "")
        if not request_id or request_id != self._approval.request_id():
            return False
        self._approval.set_status(status, reason)
        self.preferredSizeChanged.emit()
        if status in ("approved", "declined", "expired", "cancelled"):
            self._show_next_approval()
        return True

    def pending_approval_ids(self) -> list[str]:
        ids = []
        current = self._approval.request_id()
        if current and self._approval.status() in ("", "pending"):
            ids.append(current)
        ids.extend(str(q.get("request_id") or "") for q in self._approval_queue)
        return [i for i in ids if i]

    def _on_approval_decided(self, request_id: str, accepted: bool,
                             auto: bool) -> None:
        service = _approval_service()
        resolved = False
        if service is not None:
            try:
                resolved = bool(service.resolve(
                    request_id, bool(accepted), auto=bool(auto)))
            except Exception:
                resolved = False
        if not resolved:
            # The chat card may have resolved it first, or the request expired.
            status = "declined"
            try:
                if service is not None:
                    info = service.get(request_id) or {}
                    status = str(info.get("status") or status)
            except Exception:
                pass
            self._approval.set_status(
                status, "" if status != "declined" else "No approval was granted.")
        else:
            self._approval.set_status(
                "approved" if accepted else "declined",
                "You approved this action." if accepted
                else "Declined — nothing was executed.")
        self._show_next_approval()
        self.preferredSizeChanged.emit()

    def _show_next_approval(self) -> None:
        if not self._approval_queue:
            return
        nxt = self._approval_queue.pop(0)
        self._approval.set_request(nxt)
        self._approval.setVisible(True)
        self.preferredSizeChanged.emit()

    # ── internals ─────────────────────────────────────────────────────────────
    @staticmethod
    def _signature(snapshot: dict) -> tuple:
        """Fields that warrant layout work; elapsed time is updated lightweight."""
        steps = tuple(
            (str(s.get("id") or ""), str(s.get("title") or ""),
             str(s.get("status") or "pending"), str(s.get("outcome") or ""))
            for s in (snapshot.get("steps") or [])
        )
        return (
            snapshot.get("state"), snapshot.get("title"), steps,
            tuple(snapshot.get("errors") or ()), tuple(snapshot.get("warnings") or ()),
            tuple(snapshot.get("artifacts") or ()), snapshot.get("cancellable"),
            tuple(sorted((snapshot.get("answers") or {}).items())),
            snapshot.get("summary"),
        )

    def _poll_snapshots(self) -> bool:
        if not callable(self._list_provider):
            return False
        changed = False
        try:
            snapshots = list(self._list_provider() or [])
        except Exception:
            return False
        # Registry providers expose newest-first snapshots; retain oldest-first
        # insertion order internally so "next task" reliably means newest active.
        for snapshot in reversed(snapshots):
            if not isinstance(snapshot, dict):
                continue
            task_id = str(snapshot.get("id") or "")
            if not task_id:
                continue
            previous = self._snapshots.get(task_id) or {}
            if self._signature(previous) != self._signature(snapshot):
                changed = True
            self._remember(snapshot)
        return changed

    def _remember(self, snapshot: dict) -> None:
        task_id = str(snapshot.get("id") or "")
        if not task_id:
            return
        if task_id not in self._snapshots:
            self._order.append(task_id)
        self._snapshots[task_id] = dict(snapshot)
        self._sync_selector_model()

    def _sync_selector_model(self) -> None:
        ids = self._selector_ids()
        labels = [self._task_title(self._snapshots.get(task_id) or {}) for task_id in ids]
        current = [self._selector.itemText(i) for i in range(self._selector.count())]
        if labels != current:
            self._selector.blockSignals(True)
            self._selector.clear()
            self._selector.addItems(labels)
            self._selector.blockSignals(False)
        if self._selected in ids:
            self._selector.blockSignals(True)
            self._selector.setCurrentIndex(ids.index(self._selected))
            self._selector.blockSignals(False)

    def _latest_active(self) -> str:
        for task_id in reversed(self._order):
            state = str((self._snapshots.get(task_id) or {}).get("state") or "").upper()
            if state not in self._TERMINAL:
                return task_id
        return ""

    def _follow_latest_active(self) -> bool:
        """Select the newest active request, or newest record if all are done.

        Falling back to the newest record also recovers when a task is created
        and completed between two UI polls, so its final checklist/report does
        not get stranded behind an older record.
        """
        latest = self._latest_active() or (self._order[-1] if self._order else "")
        if latest and latest != self._selected:
            self._selected = latest
            return True
        return False

    def _active_ids(self) -> list[str]:
        return [task_id for task_id in self._order
                if str((self._snapshots.get(task_id) or {}).get("state") or "") not in self._TERMINAL]

    def _advance_if_needed(self) -> None:
        """On terminal completion, follow the next live task before reporting."""
        selected = self._snapshots.get(self._selected) or {}
        state = str(selected.get("state") or "")
        if state in self._TERMINAL:
            active = self._active_ids()
            if active:
                self._selected = active[-1]

    def _on_select_index(self, index: int) -> None:
        ids = self._selector_ids()
        if 0 <= index < len(ids):
            self._selected = ids[index]
            self._render()

    def _selector_ids(self) -> list[str]:
        return list(self._order)

    @staticmethod
    def _task_title(snapshot: dict) -> str:
        title = str(snapshot.get("title") or "").strip()
        if title and title.lower() not in {"untitled task", "task"}:
            return title
        request = " ".join(str(snapshot.get("request") or "").split())
        if request:
            return request[:1].upper() + request[1:]
        category = str(snapshot.get("category") or "").strip().replace("_", " ").title()
        return f"{category} task" if category else "Teardown task"

    @staticmethod
    def _display_state(snapshot: dict) -> tuple[str, str]:
        raw = str(snapshot.get("state") or "CREATED").upper()
        if raw in {"COMPLETED", "COMPLETE", "SUCCESS", "SUCCEEDED"}:
            return "COMPLETE", GREEN
        if raw in {"FAILED", "FAILURE"}:
            return "FAILED", RED
        if raw == "ERROR":
            return "ERROR", "#F4B24A"
        if raw == "CANCELLED":
            return "CANCELLED", TEXT_DIM
        if raw in {"RUNNING", "INITIALIZING", "VALIDATING", "STARTED"}:
            return "RUNNING", ACCENT
        return "PENDING", TEXT_MID

    @staticmethod
    def _format_elapsed(milliseconds: Any) -> str:
        try:
            total = max(0, int(float(milliseconds or 0) / 1000))
        except (TypeError, ValueError):
            total = 0
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    def _tick(self) -> None:
        changed = self._poll_snapshots()
        old_selected = self._selected
        # New tasks can appear in the registry even if a UI event was dropped.
        # Keep the compact checklist following the latest active record.
        self._follow_latest_active()
        self._advance_if_needed()
        if changed or old_selected != self._selected:
            self._render()
            return
        snapshot = self._snapshots.get(self._selected) or {}
        self._update_status(snapshot)
        self._spinner_index = (self._spinner_index + 1) % len(self._SPINNER)
        if self._running_mark is not None and str(snapshot.get("state") or "") not in self._TERMINAL:
            self._running_mark.setText(self._SPINNER[self._spinner_index])
        self._update_report(snapshot)

    def _render(self) -> None:
        """Render a fresh checklist from its task record, never from guessed state."""
        if self._rendering:
            return
        self._rendering = True
        try:
            self._sync_selector_model()
            snapshot = self._snapshots.get(self._selected) or {}
            if not snapshot:
                self._status.setText("WAITING")
                self._status.setStyleSheet(
                    "QLabel{color:#42C7EF;background:transparent;border:none;"
                    "border-radius:0;padding:3px 2px;font:700 7pt 'Exo 2';"
                    "letter-spacing:1px;}"
                )
                self._current.setText("Ready for a task")
                self._elapsed.setText("00:00")
                self._cancel_btn.setEnabled(False)
                self._report_btn.setEnabled(False)
                self._report_btn.hide()
                self._report_card.hide()
                self._detail.setText("")
                self._clear_steps()
                self._empty_steps.setText("Waiting for a task to be created.")
                self._empty_steps.show()
                self._update_question_visibility()
                self._update_scroll_preferred_height()
                self.updateGeometry()
                return

            self._current.setText(self._task_title(snapshot))
            self._update_status(snapshot)
            state = str(snapshot.get("state") or "").upper()
            terminal = state in self._TERMINAL
            self._cancel_btn.setEnabled(bool(snapshot.get("cancellable")) and not terminal)
            self._report_btn.setEnabled(terminal)
            self._report_btn.setVisible(terminal)

            self._clear_steps()
            steps = self._concrete_steps(snapshot)
            self._empty_steps.setVisible(not steps)
            if not steps:
                self._empty_steps.setText(
                    "No concrete execution steps were reported by the task runner."
                    if terminal else
                    "Waiting for concrete execution steps from the task runner…"
                )
            for step in steps:
                # The list ends with the empty-state label and a stretch item;
                # insert concrete operations before both.
                self._steps_lay.insertWidget(self._steps_lay.count() - 2,
                                             self._step_row(step))

            details: list[str] = []
            if snapshot.get("errors") and not self._should_show_report(snapshot):
                details.append(" · ".join(str(e) for e in snapshot["errors"][-2:]))
            if snapshot.get("warnings") and not self._should_show_report(snapshot):
                details.append(" · ".join(str(e) for e in snapshot["warnings"][-2:]))
            if snapshot.get("artifacts") and not terminal:
                details.append("Files: " + ", ".join(str(a) for a in snapshot["artifacts"][-2:]))
            self._detail.setText("\n".join(details))
            self._update_report(snapshot)
            self._update_question_visibility()
            self._update_scroll_preferred_height()
            self.updateGeometry()
        finally:
            self._rendering = False
            # The outer floating window can auto-fit after new rows/report state
            # changes, including updates discovered by polling rather than events.
            self.preferredSizeChanged.emit()

    def _update_status(self, snapshot: dict) -> None:
        label, color = self._display_state(snapshot)
        self._status.setText(label)
        # Activity-panel treatment: status is a colored label, not a rounded pill.
        self._status.setStyleSheet(
            f"QLabel{{color:{color};background:transparent;border:none;"
            "border-radius:0;padding:3px 2px;font:700 7.2pt 'Exo 2';"
            "letter-spacing:1px;}"
        )
        self._elapsed.setText(self._format_elapsed(snapshot.get("elapsed_ms", 0)))

    def _step_row(self, step: dict) -> QWidget:
        status = str(step.get("status") or "pending").lower()
        if status == "error":
            glyph, color = ("!", "#F4B24A")
        else:
            glyph, color = _STEP_GLYPH.get(status, ("○", TEXT_DIM))
        if status == "running":
            glyph = self._SPINNER[self._spinner_index]

        edge_color = QColor(color)
        if status == "running":
            fill_color = QColor(0, 38, 55, 190)
        elif status == "completed":
            fill_color = QColor(0, 34, 28, 180)
        elif status in {"failed", "error"}:
            fill_color = QColor(43, 12, 20, 190)
        else:
            fill_color = QColor(1, 15, 25, 145)
        row = _CornerLineFrame(edge=edge_color, fill=fill_color)
        # Object name and geometry stay stable; only the paint language changes.
        row.setObjectName("TaskStepCard")
        row.setStyleSheet(
            "QFrame#TaskStepCard{background:transparent;border:none;}"
            "QLabel{background:transparent;border:none;}"
        )
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        row.setMinimumWidth(0)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(9, 7, 9, 7)
        lay.setSpacing(8)
        mark = QLabel(glyph)
        mark.setFixedWidth(15)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet(f"QLabel{{color:{color};font:700 10pt 'Segoe UI Symbol';}}")
        lay.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
        if status == "running":
            self._running_mark = mark

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(1)
        title = str(step.get("title") or "Task step")
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setMinimumWidth(0)
        title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        if status == "completed":
            title_color = "#68E7B4"
        elif status in {"failed", "error"}:
            title_color = "#FF8792"
        elif status == "running":
            title_color = "#74DFFF"
        else:
            title_color = "#87B9C8"
        title_label.setStyleSheet(
            f"QLabel{{color:{title_color};font:{'600' if status == 'running' else '500'} 9pt 'Rajdhani';}}"
        )
        text_column.addWidget(title_label)
        outcome = str(step.get("outcome") or "").strip()
        if outcome:
            outcome_label = QLabel(outcome)
            outcome_label.setWordWrap(True)
            outcome_label.setMinimumWidth(0)
            outcome_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            outcome_label.setStyleSheet(
                "QLabel{color:#A2D2E0;font:500 8.2pt 'Rajdhani';}"
            )
            text_column.addWidget(outcome_label)
        lay.addLayout(text_column, 1)
        return row

    @staticmethod
    def _concrete_steps(snapshot: dict) -> list[dict]:
        """Return operation-backed steps only, excluding lifecycle bookkeeping.

        The registry keeps analyze/validate/deliver stages internally so its
        lifecycle transitions stay honest. Those universal stages are not task
        checklist items: only recorded tool dispatches, clarification steps, or
        explicitly marked concrete steps belong in the user-facing checklist.
        """
        lifecycle_ids = {"analyze", "validate", "deliver"}
        visible: list[dict] = []
        for raw in (snapshot.get("steps") or []):
            if not isinstance(raw, dict):
                continue
            step_id = str(raw.get("id") or "").strip().lower()
            metadata = raw.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            is_tool = bool(metadata.get("tool"))
            is_question = step_id == "questions" or bool(metadata.get("question_ids"))
            explicitly_visible = metadata.get("visible_checklist") is True
            is_non_lifecycle = step_id not in lifecycle_ids
            if is_tool or is_question or explicitly_visible or is_non_lifecycle:
                visible.append(raw)
        return visible

    def _update_scroll_preferred_height(self) -> None:
        """Grow to fit a short checklist, then keep longer lists scrollable."""
        try:
            desired = 64
            for index in range(max(0, self._steps_lay.count() - 2)):
                item = self._steps_lay.itemAt(index)
                row = item.widget() if item is not None else None
                # isHidden() checks this widget's own state, unlike isVisible(),
                # which is false during construction while an ancestor is hidden.
                if row is not None and not row.isHidden():
                    desired += max(28, row.sizeHint().height()) + self._steps_lay.spacing()
            target = min(360, max(72, desired))
            self._scroll.setMinimumHeight(target)
            self._scroll.setMaximumHeight(390)
            self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self._scroll.updateGeometry()
            self.updateGeometry()
        except Exception:
            pass

    def _clear_steps(self) -> None:
        self._running_mark = None
        # Keep the trailing empty-state label and stretch item. All widgets
        # before those two are concrete, registry-backed operation rows.
        while self._steps_lay.count() > 2:
            item = self._steps_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _should_show_report(self, snapshot: dict) -> bool:
        state = str(snapshot.get("state") or "").upper()
        return state in self._TERMINAL and not self._active_ids()

    def _update_report(self, snapshot: dict) -> None:
        # A report is deliberately withheld while any task remains active.
        if not snapshot or not self._should_show_report(snapshot):
            self._report_card.hide()
            self._report_detail.hide()
            self._report_btn.setText("REPORT")
            self._report_btn.setToolTip("Show the detailed report in this panel")
            return
        all_snapshots = [self._snapshots.get(tid) or {} for tid in self._order]
        states = [str(item.get("state") or "").upper() for item in all_snapshots]
        completed_tasks = sum(1 for state in states if state == "COMPLETED")
        failed_tasks = sum(1 for state in states if state in {"FAILED", "ERROR"})
        cancelled_tasks = sum(1 for state in states if state == "CANCELLED")
        concrete_steps = [
            step for item in all_snapshots for step in self._concrete_steps(item)
        ]
        completed_steps = sum(
            1 for step in concrete_steps
            if str(step.get("status") or "").lower() == "completed"
        )
        failed_steps = sum(
            1 for step in concrete_steps
            if str(step.get("status") or "").lower() in {"failed", "error"}
        )
        errors = [
            f"{self._task_title(item)}: {str(error)}"
            for item in all_snapshots for error in (item.get("errors") or [])
            if str(error).strip()
        ]
        for item, state in zip(all_snapshots, states):
            if state == "ERROR" and not item.get("errors"):
                errors.append(f"{self._task_title(item)}: An unexpected error was reported.")
        total_ms = 0
        for item in all_snapshots:
            try:
                total_ms += max(0, int(float(item.get("elapsed_ms") or 0)))
            except (TypeError, ValueError):
                continue
        self._report_metrics.setText(
            f"Completed  {completed_tasks}     Failed  {failed_tasks}     "
            f"Cancelled  {cancelled_tasks}     Errors  {len(errors)}\n"
            f"Total elapsed  {self._format_elapsed(total_ms)}\n"
            f"Concrete steps: {completed_steps} completed · {failed_steps} failed"
        )
        self._report_errors.setText("\n".join(errors[-3:]) if errors else "No errors recorded.")
        self._report_errors.setStyleSheet(
            "QLabel{color:#FF8997;font:600 8.2pt 'Rajdhani';}"
            if errors else "QLabel{color:#65E0B4;font:600 8.2pt 'Rajdhani';}"
        )
        self._report_card.show()
        self.updateGeometry()
        self.preferredSizeChanged.emit()

    def _update_question_visibility(self) -> None:
        # Keep the question flow authoritative but do not let an empty question
        # card reserve vertical space in the compact checklist.
        if self._question.isHidden() and self._question.parent() is self:
            self._question.setMaximumHeight(0)
        else:
            self._question.setMaximumHeight(16777215)

    def show_report_details(self, text: str) -> None:
        """Render the real report as Markdown inside the blue checklist card."""
        markdown = str(text or "No detailed report is available.")
        # Qt's Markdown renderer handles headings, lists, tables, code blocks,
        # links and emphasis without creating a second window.
        self._report_detail.setMarkdown(markdown)
        self._report_detail.document().setDefaultStyleSheet(
            "body { color: #DDF7FF; font-family: 'Rajdhani'; font-size: 10pt; }"
            "h1,h2,h3,h4 { color: #73E6FF; margin-top: 8px; margin-bottom: 4px; }"
            "p,li { color: #B9E8F5; } a { color: #65DFFB; }"
            "pre,code { background-color: #0A2A3A; color: #C5F1FF; }"
            "blockquote { color: #82B7C8; border-left: 3px solid #2C8BA6; margin-left: 6px; padding-left: 8px; }"
            "table { border-collapse: collapse; } th,td { border: 1px solid #235568; padding: 4px 6px; }"
        )
        self._report_detail.viewport().setAutoFillBackground(False)
        self._report_detail.viewport().setStyleSheet(
            "background:transparent;border:none;"
        )
        self._report_detail.show()
        self._report_card.show()
        self._report_btn.setText("HIDE REPORT")
        self._report_btn.setToolTip("Hide the detailed Markdown report")
        self._report_card.updateGeometry()
        self.updateGeometry()
        self.preferredSizeChanged.emit()

    def _on_report_clicked(self) -> None:
        if self._report_detail.isVisible():
            self._report_detail.hide()
            self._report_btn.setText("REPORT")
            self._report_btn.setToolTip("Show the detailed report in this panel")
            self._report_card.updateGeometry()
            self.updateGeometry()
            self.preferredSizeChanged.emit()
            return
        task_id = self._selected
        if not task_id:
            return
        text = ""
        if callable(self._report_provider):
            try:
                text = self._report_provider(task_id) or ""
            except Exception:
                text = ""
        if callable(self._on_report):
            self._on_report(task_id, text)
        else:
            self.show_report_details(text)

    def _on_cancel_clicked(self) -> None:
        snapshot = self._snapshots.get(self._selected) or {}
        if (self._selected and bool(snapshot.get("cancellable"))
                and callable(self._on_cancel)):
            self._on_cancel(self._selected)

