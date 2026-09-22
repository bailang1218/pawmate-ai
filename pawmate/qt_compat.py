"""Qt binding compatibility layer for PawMate.

PySide6 is the only supported Qt binding for distribution.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QCoreApplication,
    QDir,
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    QProcess,
    QRect,
    QSize,
    QThread,
    QTimer,
    QUrl,
    Qt,
    SIGNAL as _qt_signal,
    Signal,
    Slot,
)
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def __getattr__(name: str):
    """Lazy-load QtWebEngine symbols so app startup does not pay for Chromium."""
    if name == "QWebEngineSettings":
        from PySide6.QtWebEngineCore import QWebEngineSettings

        globals()[name] = QWebEngineSettings
        return QWebEngineSettings
    if name == "QWebEngineView":
        from PySide6.QtWebEngineWidgets import QWebEngineView

        globals()[name] = QWebEngineView
        return QWebEngineView
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def qt_receiver_count(obj: QObject, signal, signature: str) -> int:
    """Return a signal's receiver count under PySide6."""
    try:
        return int(obj.receivers(signal))
    except (TypeError, ValueError):
        return int(obj.receivers(_qt_signal(signature)))


__all__ = [
    "QApplication",
    "QBrush",
    "QCheckBox",
    "QColor",
    "QComboBox",
    "QCoreApplication",
    "QDialog",
    "QDir",
    "QElapsedTimer",
    "QEvent",
    "QFileDialog",
    "QGraphicsDropShadowEffect",
    "QGroupBox",
    "QGuiApplication",
    "QHBoxLayout",
    "QIcon",
    "QLabel",
    "QLineEdit",
    "QMainWindow",
    "QMessageBox",
    "QObject",
    "QPainter",
    "QPen",
    "QPixmap",
    "QPolygon",
    "QPoint",
    "QProcess",
    "QPushButton",
    "QRect",
    "QSize",
    "QSizePolicy",
    "QThread",
    "QTimer",
    "QUrl",
    "QVBoxLayout",
    "QWebChannel",
    "QWebEngineSettings",
    "QWebEngineView",
    "QWidget",
    "Qt",
    "Signal",
    "Slot",
    "qt_receiver_count",
]
