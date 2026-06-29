"""Native Qt styling for PawMate fallback dialogs.

Qt widgets use QSS rather than browser CSS, but the tokens below mirror the
current Web UI palette so unavoidable native dialogs still feel like PawMate.
"""
from __future__ import annotations

from typing import Any


_STYLE_MARKER = "/* PawMate native Qt style */"

PAWMATE_NATIVE_QSS = """
/* PawMate native Qt style */
QWidget {
    font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 13px;
    color: #1f2d35;
}

QDialog, QMessageBox, QFileDialog {
    background: #f8fbfc;
    color: #1f2d35;
}

QMessageBox QLabel {
    color: #1f2d35;
    font-size: 13px;
    line-height: 1.45;
}

QGroupBox {
    background: #ffffff;
    border: 1px solid #d7edf5;
    border-radius: 10px;
    margin-top: 14px;
    padding: 18px 12px 12px 12px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    top: 2px;
    padding: 0 6px;
    color: #6b7c85;
    font-size: 12px;
    font-weight: 600;
}

QLabel {
    color: #1f2d35;
}

QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QSpinBox {
    background: #ffffff;
    border: 1px solid #d7edf5;
    border-radius: 8px;
    padding: 8px 10px;
    min-height: 20px;
    color: #1f2d35;
    selection-background-color: #5db3cf;
    selection-color: #ffffff;
}

QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus {
    border: 1px solid #5db3cf;
}

QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled {
    background: #edf5f8;
    color: #8aa0aa;
}

QComboBox::drop-down {
    width: 28px;
    border: none;
}

QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #d7edf5;
    border-radius: 8px;
    outline: none;
    selection-background-color: #e7f5fa;
    selection-color: #2d7d99;
}

QCheckBox {
    color: #1f2d35;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #b9ddea;
    border-radius: 4px;
    background: #ffffff;
}

QCheckBox::indicator:checked {
    background: #5db3cf;
    border: 1px solid #5db3cf;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #cbe7f2;
    border-radius: 8px;
    padding: 8px 16px;
    color: #1f2d35;
    font-weight: 600;
    min-height: 18px;
}

QPushButton:hover {
    background: #f3fafd;
    border-color: #9bd4e6;
}

QPushButton:pressed {
    background: #e7f5fa;
}

QPushButton:default, QPushButton#primaryAction {
    background: #5db3cf;
    border: 1px solid #5db3cf;
    color: #ffffff;
}

QPushButton:default:hover, QPushButton#primaryAction:hover {
    background: #67c3de;
    border-color: #67c3de;
}

QPushButton#secondaryAction {
    background: #f8fbfc;
    color: #5a7682;
}

QPushButton:disabled {
    background: #edf5f8;
    border-color: #d7edf5;
    color: #9aadb6;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px;
}

QScrollBar::handle:vertical {
    background: #c7e3ee;
    border-radius: 5px;
    min-height: 28px;
}

QScrollBar::handle:vertical:hover {
    background: #9bd4e6;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QFileDialog QListView, QFileDialog QTreeView {
    background: #ffffff;
    border: 1px solid #d7edf5;
    border-radius: 8px;
}
"""


def apply_native_style(target: Any) -> None:
    """Append the PawMate QSS to a QApplication or QWidget once."""
    if target is None or not hasattr(target, "setStyleSheet"):
        return
    current = target.styleSheet() if hasattr(target, "styleSheet") else ""
    if _STYLE_MARKER in current:
        return
    target.setStyleSheet((current.rstrip() + "\n\n" + PAWMATE_NATIVE_QSS).strip())
