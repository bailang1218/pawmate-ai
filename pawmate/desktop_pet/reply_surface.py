from __future__ import annotations

import logging
from typing import Callable, Literal

ReplySurface = Literal["window", "bubble"]


class ReplySurfaceController:
    def __init__(
        self,
        *,
        pet_enabled: Callable[[], bool],
        surface: Callable[[], str],
        submit_fn: Callable[[str], object],
    ) -> None:
        self._pet_enabled = pet_enabled
        self._surface = surface
        self._submit_fn = submit_fn
        self._logger = logging.getLogger("pawmate")

    def on_user_submit(self, text: str, source: str) -> object:
        self._logger.debug("[ReplySurface] submit source=%s chars=%d", source, len(text))
        return self._submit_fn(text)

    def should_show_bubble(self) -> bool:
        return self._pet_enabled() and self._normalized_surface() == "bubble"

    def should_pop_window(self) -> bool:
        return not self.should_show_bubble()

    def _normalized_surface(self) -> ReplySurface:
        return "bubble" if self._surface() == "bubble" else "window"
