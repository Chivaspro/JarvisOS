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

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPalette
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSizePolicy, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
    QStyle, QStyleOptionButton,
)

from ui_theme import (
    ACCENT, ACCENT_TEXT, AMBER, BORDER, BORDER_SOFT, CARD, CARD_HI, FIELD_BG,
    FONT_HEAD, FONT_MONO, FONT_SMALL, FONT_UI, GREEN, RADIUS_MD, RADIUS_SM,
    RED, TEXT, TEXT_BRIGHT, TEXT_DIM, TEXT_MID, rgba, scrollbar_css,
)

# This panel owns its palette rather than inheriting the HUD's neon colors.
# The shell remains translucent, with light-blue surfaces and restrained status tones.
ACCENT = "#438BCB"
ACCENT_TEXT = "#FFFFFF"
AMBER = "#B67B2C"
BORDER = "#BDD7EE"
BORDER_SOFT = "#DCEAF6"
CARD = "#F5FAFF"
CARD_HI = "#EAF4FD"
FIELD_BG = "#F7FBFF"
GREEN = "#2E8A68"
RED = "#C05260"
TEXT = "#35516B"
TEXT_BRIGHT = "#173B59"
TEXT_DIM = "#91A8BC"
TEXT_MID = "#617E98"
FONT_HEAD = "Segoe UI"
FONT_MONO = "Consolas"
FONT_UI = "Segoe UI"
FONT_SMALL = "Segoe UI"

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

    _CHECKED = QColor("#347FBD")
    _CHECKED_BORDER = QColor("#215F94")
    _UNCHECKED = QColor("#E4F1FC")
    _UNCHECKED_HOVER = QColor("#D2E8FA")
    _BORDER = QColor("#8FB8D9")
    _CHECK = QColor("#FFFFFF")

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


def _btn(text: str, *, accent: bool = False, danger: bool = False,
         compact: bool = False) -> QPushButton:
    b = QPushButton(text)
    color = ACCENT if accent else (RED if danger else TEXT_MID)
    hover = ACCENT_TEXT if accent else (RED if danger else ACCENT)
    pad = "4px 10px" if compact else "6px 14px"
    b.setStyleSheet(
        f"QPushButton{{background:{rgba(CARD_HI, 200)};color:{color};"
        f"border:1px solid {BORDER};border-radius:{RADIUS_SM}px;"
        f"padding:{pad};font:700 8pt {FONT_UI};letter-spacing:0.6px;}}"
        f"QPushButton:hover{{color:{hover};border:1px solid {hover};background:rgba(255,255,255,238);}}"
        f"QPushButton:pressed{{background:rgba(202,229,249,230);border:1px solid {ACCENT};}}"
        f"QPushButton:disabled{{color:{TEXT_DIM};border:1px solid {BORDER_SOFT};background:rgba(244,249,253,145);}}"
    )
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


# ── question flow ─────────────────────────────────────────────────────────────

class QuestionFlowView(QFrame):
    """One question (or the summary) of an open clarification flow."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task_id = ""
        self._snapshot: dict = {}
        self._selected: list[str] = []
        self._checks: dict[str, QCheckBox] = {}
        self._build()

    def _build(self) -> None:
        self.setStyleSheet(
            f"QFrame{{background:{rgba(CARD, 220)};border:1px solid {BORDER};"
            f"border-radius:{RADIUS_MD}px;}}"
            f"QLabel{{background:transparent;border:none;}}"
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
        self._custom.setStyleSheet(
            f"QLineEdit{{background:{rgba(FIELD_BG, 200)};color:{TEXT};"
            f"border:1px solid {BORDER};border-radius:{RADIUS_SM}px;"
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
        current = self._snapshot.get("answered") or {}
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
            box.setMinimumHeight(26)
            box.setChecked(oid in self._answers_for(str(question.get("id") or "")))
            box.stateChanged.connect(
                lambda _s, _o=oid, _m=multi: self._on_option(_o, _m))
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
        if multi:
            if box.isChecked() and option_id not in self._selected:
                self._selected.append(option_id)
            elif not box.isChecked() and option_id in self._selected:
                self._selected.remove(option_id)
        else:
            self._selected = [option_id] if box.isChecked() else []
            for oid, other in self._checks.items():
                if oid != option_id and other.isChecked():
                    other.blockSignals(True)
                    other.setChecked(False)
                    other.blockSignals(False)
        self._sync_selected()

    def _sync_selected(self) -> None:
        # The summary/next action state follows the REAL selection.
        pass

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


# ── task progress pane ────────────────────────────────────────────────────────

class TaskProgressPane(QWidget):
    """Compact, glass-like checklist driven by live task-registry snapshots."""

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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        # Use a real tinted pane fallback instead of allowing platform-native
        # QWidget/viewport defaults to leak through as white.
        pane_palette = self.palette()
        pane_palette.setColor(QPalette.ColorRole.Window, QColor("#D7EAFB"))
        self.setPalette(pane_palette)
        self.setStyleSheet(
            "QWidget#TeardownChecklistPane{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #E4F2FF,stop:0.55 #D2E8FA,stop:1 #C4DFF5);border:none;}"
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
            "QLabel{color:#438BCB;background:rgba(222,240,255,190);"
            "border:1px solid rgba(125,176,220,135);border-radius:12px;"
            "padding:3px 10px;font:700 8pt 'Segoe UI';letter-spacing:0.5px;}"
        )
        toolbar.addWidget(self._status, 0, Qt.AlignmentFlag.AlignLeft)
        toolbar.addStretch(1)
        self._elapsed = QLabel("00:00")
        self._elapsed.setObjectName("taskElapsed")
        self._elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._elapsed.setStyleSheet(
            "QLabel{color:#476A88;font:600 10pt 'Consolas';padding:2px 4px;}"
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
            "QLabel{color:#173B59;font:650 12pt 'Segoe UI';padding:1px 2px;}"
        )
        self._current.setMinimumWidth(0)
        self._current.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lay.addWidget(self._current)

        self._question = QuestionFlowView()
        self._question.setVisible(False)
        lay.addWidget(self._question)

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
            "QLabel#taskChecklistEmptyState{color:#557B9A;"
            "background:rgba(205,229,249,115);border:1px dashed #8FB8DA;"
            "border-radius:10px;padding:12px;font:400 9pt 'Segoe UI';}"
        )
        self._steps_lay.addWidget(self._empty_steps)
        self._steps_lay.addStretch(1)
        self._scroll.setWidget(self._steps_host)
        lay.addWidget(self._scroll, 1)

        self._report_card = QFrame(self)
        self._report_card.setObjectName("TaskReportCard")
        self._report_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._report_card.setAutoFillBackground(True)
        report_palette = self._report_card.palette()
        report_palette.setColor(QPalette.ColorRole.Window, QColor("#CBE4F9"))
        self._report_card.setPalette(report_palette)
        self._report_card.setStyleSheet(
            "QFrame#TaskReportCard{"
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "stop:0 #D8ECFF,stop:0.50 #C4E1FA,stop:1 #AFCFEF);"
            "border:1px solid #78ACD9;border-radius:12px;}"
            "QLabel{background:transparent;border:none;}"
        )
        self._report_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._report_card.setMinimumWidth(0)
        report_lay = QVBoxLayout(self._report_card)
        report_lay.setContentsMargins(11, 9, 11, 9)
        report_lay.setSpacing(4)
        self._report_title = QLabel("Teardown report")
        self._report_title.setStyleSheet(
            "QLabel{color:#16466F;font:750 9pt 'Segoe UI';}"
        )
        report_lay.addWidget(self._report_title)
        self._report_metrics = QLabel("")
        self._report_metrics.setWordWrap(True)
        self._report_metrics.setMinimumWidth(0)
        self._report_metrics.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._report_metrics.setStyleSheet(
            "QLabel{color:#244E70;font:550 8.5pt 'Segoe UI';}"
        )
        report_lay.addWidget(self._report_metrics)
        self._report_errors = QLabel("")
        self._report_errors.setWordWrap(True)
        self._report_errors.setStyleSheet(
            "QLabel{color:#8D5860;font:400 8pt 'Segoe UI';}"
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
        self._report_detail.setAutoFillBackground(True)
        self._report_detail.setFrameShape(QFrame.Shape.NoFrame)
        report_text_palette = self._report_detail.palette()
        report_text_palette.setColor(QPalette.ColorRole.Base, QColor("#DFF0FF"))
        report_text_palette.setColor(QPalette.ColorRole.Text, QColor("#183F60"))
        report_text_palette.setColor(QPalette.ColorRole.Window, QColor("#DFF0FF"))
        self._report_detail.setPalette(report_text_palette)
        report_css = (
            "QTextBrowser#TaskReportDetails{"
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 #E7F4FF,stop:1 #CFE6FA);color:#183F60;"
            "border:1px solid #80B5E1;border-radius:8px;padding:8px;"
            "selection-background-color:#A9D0F1;}"
            "QScrollBar:vertical{width:7px;background:#D5E9FA;margin:2px;border:none;}"
            "QScrollBar::handle:vertical{background:#75A9D7;border-radius:3px;min-height:22px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        self._report_detail.setStyleSheet(report_css)
        # QTextBrowser paints its viewport separately; styling only the outer
        # frame leaves that child white on some Qt platform styles.
        self._report_detail.viewport().setAutoFillBackground(True)
        self._report_detail.viewport().setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #E7F4FF,stop:1 #CFE6FA);border:none;"
        )
        self._report_detail.document().setDefaultStyleSheet(
            "body { color: #183F60; font-family: 'Segoe UI'; font-size: 10pt; }"
            "h1,h2,h3,h4 { color: #174D7A; margin-top: 8px; margin-bottom: 4px; }"
            "p,li { color: #234E70; }"
            "a { color: #176FB3; text-decoration: underline; }"
            "pre,code { background-color: #C4DFF5; color: #17466B; }"
            "blockquote { color: #476F8F; border-left: 3px solid #78ACD9; margin-left: 6px; padding-left: 8px; }"
            "table { border-collapse: collapse; } th,td { border: 1px solid #9ABFDF; padding: 4px 6px; }"
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
            "QLabel{color:#67839B;font:400 8pt 'Segoe UI';background:transparent;}"
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
        task_id = str(snapshot.get("task_id") or "")
        if task_id:
            self._selected = task_id
            self._render()

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
            return "ERROR", "#B15C2D"
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
                    "QLabel{color:#438BCB;background:rgba(222,240,255,190);"
                    "border:1px solid rgba(125,176,220,135);border-radius:12px;"
                    "padding:3px 10px;font:700 8pt 'Segoe UI';letter-spacing:0.5px;}"
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
        finally:
            self._rendering = False

    def _update_status(self, snapshot: dict) -> None:
        label, color = self._display_state(snapshot)
        self._status.setText(label)
        bg = "rgba(222,240,255,195)"
        if label == "COMPLETE":
            bg = "rgba(222,246,237,205)"
        elif label in {"FAILED", "ERROR"}:
            bg = "rgba(255,232,234,205)"
        elif label == "CANCELLED":
            bg = "rgba(235,241,247,205)"
        outline = "rgba(125,176,220,145)"
        if label == "COMPLETE":
            outline = "rgba(106,177,151,155)"
        elif label in {"FAILED", "ERROR"}:
            outline = "rgba(210,137,147,165)"
        elif label == "CANCELLED":
            outline = "rgba(173,194,211,145)"
        self._status.setStyleSheet(
            f"QLabel{{color:{color};background:{bg};border:1px solid {outline};"
            "border-radius:12px;padding:3px 10px;font:700 8pt 'Segoe UI';"
            "letter-spacing:0.5px;}"
        )
        self._elapsed.setText(self._format_elapsed(snapshot.get("elapsed_ms", 0)))

    def _step_row(self, step: dict) -> QWidget:
        status = str(step.get("status") or "pending").lower()
        if status == "error":
            glyph, color = ("!", "#B15C2D")
        else:
            glyph, color = _STEP_GLYPH.get(status, ("○", TEXT_DIM))
        if status == "running":
            glyph = self._SPINNER[self._spinner_index]

        row = QFrame()
        # Stable object name makes the row's own rule win over global HUD QSS.
        row.setObjectName("TaskStepCard")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.setAutoFillBackground(True)
        if status == "running":
            background = "qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #D9EEFF,stop:1 #B9DDF8)"
            border = "#78B1E2"
        elif status == "completed":
            background = "qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #E1F5F2,stop:1 #C8EAE5)"
            border = "#9ACFC7"
        elif status in {"failed", "error"}:
            background = "qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #FFE9EC,stop:1 #F7D2D9)"
            border = "#DDA2AC"
        elif status == "waiting":
            background = "qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #EAF4FD,stop:1 #D7E9F9)"
            border = "#B6D2EC"
        else:
            background = "qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #E5F2FD,stop:1 #D0E6F8)"
            border = "#A9CBE8"
        # Palette is a fallback for styles that do not paint QFrame backgrounds;
        # the object-specific gradient provides the normal look.
        row_palette = row.palette()
        if status == "running":
            fallback_color = "#C6E4FC"
        elif status == "completed":
            fallback_color = "#D3EEE9"
        elif status in {"failed", "error"}:
            fallback_color = "#F9DDE2"
        else:
            fallback_color = "#D9EBFA"
        row_palette.setColor(QPalette.ColorRole.Window, QColor(fallback_color))
        row.setPalette(row_palette)
        row.setStyleSheet(
            f"QFrame#TaskStepCard{{background:{background};border:1px solid {border};border-radius:10px;}}"
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
            title_color = "#2E705D"
        elif status in {"failed", "error"}:
            title_color = "#9D4651"
        elif status == "running":
            title_color = "#245D8A"
        else:
            title_color = "#58748D"
        title_label.setStyleSheet(
            f"QLabel{{color:{title_color};font:{'600' if status == 'running' else '500'} 9pt 'Segoe UI';}}"
        )
        text_column.addWidget(title_label)
        outcome = str(step.get("outcome") or "").strip()
        if outcome:
            outcome_label = QLabel(outcome)
            outcome_label.setWordWrap(True)
            outcome_label.setMinimumWidth(0)
            outcome_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            outcome_label.setStyleSheet(
                "QLabel{color:#456985;font:400 8.2pt 'Segoe UI';}"
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
            "QLabel{color:#923E4C;font:500 8.2pt 'Segoe UI';}"
            if errors else "QLabel{color:#286B61;font:500 8.2pt 'Segoe UI';}"
        )
        self._report_card.show()

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
            "body { color: #183F60; font-family: 'Segoe UI'; font-size: 10pt; }"
            "h1,h2,h3,h4 { color: #174D7A; margin-top: 8px; margin-bottom: 4px; }"
            "p,li { color: #234E70; } a { color: #176FB3; }"
            "pre,code { background-color: #C4DFF5; color: #17466B; }"
            "blockquote { color: #476F8F; border-left: 3px solid #78ACD9; margin-left: 6px; padding-left: 8px; }"
            "table { border-collapse: collapse; } th,td { border: 1px solid #9ABFDF; padding: 4px 6px; }"
        )
        self._report_detail.viewport().setAutoFillBackground(True)
        self._report_detail.viewport().setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #E7F4FF,stop:1 #CFE6FA);border:none;"
        )
        self._report_detail.show()
        self._report_card.show()
        self._report_btn.setText("HIDE REPORT")
        self._report_btn.setToolTip("Hide the detailed Markdown report")
        self._report_card.updateGeometry()
        self.updateGeometry()

    def _on_report_clicked(self) -> None:
        if self._report_detail.isVisible():
            self._report_detail.hide()
            self._report_btn.setText("REPORT")
            self._report_btn.setToolTip("Show the detailed report in this panel")
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

