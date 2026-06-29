"""
QWebChannel bridge for window-level operations.

Registered as "windowBridge" in QWebChannel so that
JS can call Python slots for window controls and settings.
"""
from __future__ import annotations

import logging

from pawmate.qt_compat import QObject, Signal, Slot

logger = logging.getLogger("pawmate")


class WindowBridge(QObject):

    closeRequested = Signal()
    minimizeRequested = Signal()
    maximizeRestoreRequested = Signal()

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
