"""Browser surface state: target browser x perception modality."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Target(str, Enum):
    NATIVE = "native"
    MANAGED = "managed"


class Modality(str, Enum):
    DOM = "dom"
    VISION = "vision"


@dataclass
class BrowserSurface:
    target: Target = Target.MANAGED
    modality: Modality = Modality.DOM

    def label(self) -> str:
        return f"{self.target.value}/{self.modality.value}"

    def with_(self, *, target: Target | None = None, modality: Modality | None = None) -> "BrowserSurface":
        return BrowserSurface(
            target=target if target is not None else self.target,
            modality=modality if modality is not None else self.modality,
        )
