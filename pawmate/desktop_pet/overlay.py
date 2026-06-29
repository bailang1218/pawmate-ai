from __future__ import annotations

import re
from typing import Callable

from pawmate.qt_compat import (
    QBrush,
    QColor,
    QGraphicsDropShadowEffect,
    QLabel,
    QLineEdit,
    QPainter,
    QPen,
    QPoint,
    QPolygon,
    QRect,
    QTimer,
    Qt,
    QVBoxLayout,
    QWidget,
)


# Mirrored from pawmate/ui/web/css/tokens.css and chat.css.
AI_BUBBLE_BG = QColor(242, 250, 253, 246)  # --ai-bubble: #f2fafd
AI_BUBBLE_BORDER = QColor(215, 237, 245, 235)  # --border-strong: #d7edf5
TEXT_COLOR = "#1f2d35"  # --text
TEXT_MUTED_COLOR = "#6b7c85"  # --text-muted
ACCENT_COLOR = "#5db3cf"  # --user-bubble / focus accent
BUBBLE_RADIUS = 14  # --radius
BUBBLE_PADDING_X = 15  # .bubble padding-inline
BUBBLE_PADDING_Y = 12  # .bubble padding-block


class BubbleOverlay(QWidget):
    def __init__(
        self,
        *,
        clear_after_ms: int = 5000,
        page_interval_ms: int = 2600,
        on_show_all: Callable[[], object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(0.98)

        self._text = ""
        self._pages: list[str] = []
        self._page_index = 0
        self._on_show_all = on_show_all
        self._background_alpha = AI_BUBBLE_BG.alpha()
        self._max_body_height = 150
        self._tail_height = 10
        self._tail_width = 20
        self._stroke_width = 1
        self._max_overlay_height = self._max_body_height + (BUBBLE_PADDING_Y * 2) + self._tail_height + 8
        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self.clear)
        self._page_timer = QTimer(self)
        self._page_timer.setSingleShot(False)
        self._page_timer.timeout.connect(self._advance_page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(BUBBLE_PADDING_X, BUBBLE_PADDING_Y, BUBBLE_PADDING_X, BUBBLE_PADDING_Y + self._tail_height)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setMinimumWidth(220)
        self.label.setMaximumWidth(360)
        self.label.setMaximumHeight(self._max_body_height)
        self.label.setStyleSheet(
            "QLabel {"
            f" color: {TEXT_COLOR};"
            " background: transparent;"
            " border: none;"
            " padding: 0;"
            " font-family: 'Segoe UI', 'Microsoft YaHei UI', Arial, sans-serif;"
            " font-size: 13px;"
            " line-height: 1.55;"
            "}"
        )
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(31, 45, 53, 58))
        self.setGraphicsEffect(shadow)
        self.setStyleSheet("BubbleOverlay { background: transparent; }")
        self.setMaximumHeight(self._max_overlay_height)
        layout.addWidget(self.label)
        self._clear_after_ms = clear_after_ms
        self._page_interval_ms = page_interval_ms
        self.hide()

    @property
    def background_alpha(self) -> int:
        return self._background_alpha

    @property
    def max_overlay_height(self) -> int:
        return self._max_overlay_height

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def begin_stream(self) -> None:
        self._clear_timer.stop()
        self._page_timer.stop()
        self._text = ""
        self._pages = []
        self._page_index = 0
        self.label.setText("")
        self.hide()

    def append_text(self, delta: str) -> None:
        if not delta:
            return
        self._clear_timer.stop()
        self._page_timer.stop()
        self._text += delta
        self._pages = self._paginate(self._text)
        self._page_index = max(0, len(self._pages) - 1)
        self._render_current_page()
        if not self.isVisible():
            self.show()

    def finalize(self) -> None:
        if not self._text:
            self.clear()
            return
        self._pages = self._paginate(self._text)
        self._page_index = 0
        self._render_current_page()
        if len(self._pages) > 1:
            self._page_timer.start(self._page_interval_ms)
        delay_ms = max(self._clear_after_ms, min(12000, self._clear_after_ms + len(self._text) * 35))
        self._clear_timer.start(delay_ms)

    def clear(self) -> None:  # type: ignore[override]
        self._clear_timer.stop()
        self._page_timer.stop()
        self._text = ""
        self._pages = []
        self._page_index = 0
        self.label.setText("")
        self.hide()

    def reposition(self, pet_geom: QRect) -> None:
        self.adjustSize()
        x = pet_geom.x() + round((pet_geom.width() - self.width()) / 2)
        y = pet_geom.y() - self.height() - 8
        self.move(max(0, x), max(0, y))

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if callable(self._on_show_all):
            self._on_show_all()
        QWidget.mousePressEvent(self, event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        QWidget.paintEvent(self, event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        margin = 2
        body = self.rect().adjusted(margin, margin, -margin, -(self._tail_height + margin))
        if body.width() <= 0 or body.height() <= 0:
            return

        tail_center = body.center().x()
        tail_half = self._tail_width // 2
        tail_left = QPoint(tail_center - tail_half, body.bottom() - 1)
        tail_right = QPoint(tail_center + tail_half, body.bottom() - 1)
        tail_tip = QPoint(tail_center, body.bottom() + self._tail_height)
        tail = QPolygon([tail_left, tail_right, tail_tip])

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(AI_BUBBLE_BG))
        painter.drawRoundedRect(body, BUBBLE_RADIUS, BUBBLE_RADIUS)
        painter.drawPolygon(tail)

        painter.setPen(QPen(AI_BUBBLE_BORDER, self._stroke_width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(body, BUBBLE_RADIUS, BUBBLE_RADIUS)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(AI_BUBBLE_BG))
        painter.drawPolygon(tail)

        painter.setPen(QPen(AI_BUBBLE_BORDER, self._stroke_width))
        painter.drawLine(tail_left, tail_tip)
        painter.drawLine(tail_tip, tail_right)

    def _render_current_page(self) -> None:
        if not self._pages:
            self.label.setText("")
            return
        page = self._pages[min(self._page_index, len(self._pages) - 1)]
        suffix = f"\n\n{self._page_index + 1}/{len(self._pages)}  点击查看全文" if len(self._pages) > 1 else ""
        self.label.setText(page + suffix)

    def _advance_page(self) -> None:
        if self._page_index >= len(self._pages) - 1:
            self._page_timer.stop()
            return
        self._page_index += 1
        self._render_current_page()

    @staticmethod
    def _paginate(text: str, *, chars_per_page: int = 170) -> list[str]:
        clean = text.strip()
        if not clean:
            return []
        chunks = [chunk for chunk in re.split(r"(?<=[。！？.!?])\s+|\n{2,}", clean) if chunk]
        if not chunks:
            chunks = [clean]
        pages: list[str] = []
        current = ""
        for chunk in chunks:
            parts = [chunk[i : i + chars_per_page] for i in range(0, len(chunk), chars_per_page)] or [chunk]
            for part in parts:
                candidate = f"{current}\n{part}" if current else part
                if len(candidate) <= chars_per_page:
                    current = candidate
                else:
                    if current:
                        pages.append(current)
                    current = part
        if current:
            pages.append(current)
        return pages


class PetInputBar(QWidget):
    def __init__(self, *, on_submit: Callable[[str], object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(0.98)
        self._on_submit = on_submit

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("和 PawMate 说点什么")
        self.input.setMinimumWidth(220)
        self.input.setStyleSheet(
            "QLineEdit {"
            f" color: {TEXT_COLOR};"
            " background: rgba(242, 250, 253, 246);"
            " border: 1px solid rgba(215, 237, 245, 235);"
            f" border-radius: {BUBBLE_RADIUS}px;"
            " padding: 9px 12px;"
            " font-family: 'Segoe UI', 'Microsoft YaHei UI', Arial, sans-serif;"
            " font-size: 13px;"
            " line-height: 1.45;"
            " selection-background-color: rgba(93, 179, 207, 120);"
            f" placeholder-text-color: {TEXT_MUTED_COLOR};"
            "}"
            "QLineEdit:focus {"
            " border-color: rgba(93, 179, 207, 153);"
            "}"
        )
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(31, 45, 53, 46))
        self.input.setGraphicsEffect(shadow)
        self.input.returnPressed.connect(self._submit_current_text)
        self.input.keyPressEvent = self._input_key_press_event  # type: ignore[method-assign]
        self.input.focusOutEvent = self._input_focus_out_event  # type: ignore[method-assign]
        layout.addWidget(self.input)
        self.hide()

    def show_bar(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def hide_bar(self) -> None:
        self.hide()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide_bar()
        else:
            self.show_bar()

    def reposition(self, pet_geom: QRect) -> None:
        self.adjustSize()
        x = pet_geom.x() + round((pet_geom.width() - self.width()) / 2)
        y = pet_geom.y() + pet_geom.height() + 8
        self.move(max(0, x), max(0, y))

    def _submit_current_text(self) -> None:
        text = self.input.text()
        if not text.strip():
            return
        self.input.clear()
        self._on_submit(text)

    def _input_key_press_event(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.input.clear()
            return
        QLineEdit.keyPressEvent(self.input, event)

    def _input_focus_out_event(self, event) -> None:
        QLineEdit.focusOutEvent(self.input, event)
