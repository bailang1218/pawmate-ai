from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PetController:
    """Small facade for future PawMate/agent integration.

    The controller intentionally does not own a second application main loop.
    When integrated into ``pawmate.main``, it should be created after the shared
    QApplication exists and then route events into the viewer/state machine.
    """

    viewer: Any | None = None

    def emit(self, event_name: str, payload: dict[str, Any] | None = None) -> None:
        if self.viewer is None:
            return
        trigger = getattr(self.viewer, "trigger_event", None)
        if trigger is not None:
            trigger(event_name)

    def attach_viewer(self, viewer: Any) -> None:
        self.viewer = viewer

