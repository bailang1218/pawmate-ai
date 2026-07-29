"""
QWebChannel bridge for window-level operations.

Registered as "windowBridge" in QWebChannel so that
JS can call Python slots for window controls and settings.
"""
from __future__ import annotations

import logging

from PySide6.QtGui import QDesktopServices

from pawmate.qt_compat import QObject, QUrl, Signal, Slot

logger = logging.getLogger("pawmate")


class WindowBridge(QObject):

    closeRequested = Signal()
    minimizeRequested = Signal()
    maximizeRestoreRequested = Signal()
    browserHostGeometryRequested = Signal(int, int, int, int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        logger.info("[WindowBridge] initialized (parent=%s)", parent.__class__.__name__ if parent else "None")

    # ============================================================
    # Slots callable from JS via QWebChannel
    # ============================================================

    @Slot()
    def closeWindow(self) -> None:
        logger.info("[WindowBridge] closeWindow called")
        self.closeRequested.emit()

    @Slot()
    def minimizeWindow(self) -> None:
        logger.info("[WindowBridge] minimizeWindow called")
        self.minimizeRequested.emit()

    @Slot()
    def maximizeRestoreWindow(self) -> None:
        logger.info("[WindowBridge] maximizeRestoreWindow called")
        self.maximizeRestoreRequested.emit()

    @Slot(int, int, int, int, bool)
    def setBrowserHostGeometry(self, x: int, y: int, width: int, height: int, visible: bool) -> None:
        """Mirror the browser-stage DOM rectangle into the native Qt layer."""
        safe_width = max(0, min(int(width), 10_000))
        safe_height = max(0, min(int(height), 10_000))
        self.browserHostGeometryRequested.emit(
            max(0, int(x)),
            max(0, int(y)),
            safe_width,
            safe_height,
            bool(visible and safe_width > 0 and safe_height > 0),
        )

    @Slot(str)
    def openExternalUrl(self, url: str) -> None:
        target = QUrl(str(url or "").strip())
        if target.scheme().lower() not in {"http", "https"}:
            logger.warning("[WindowBridge] rejected external URL: %s", url)
            return
        opened = QDesktopServices.openUrl(target)
        logger.info("[WindowBridge] openExternalUrl opened=%s url=%s", opened, target.toString())
