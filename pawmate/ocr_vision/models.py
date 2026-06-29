"""Data models for OCR vision results and configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class VisionStorageConfig:
    """Runtime storage settings for vision artifacts."""

    root_dir: Path
    temp_dir_name: str = "tmp"
    keep_days: int = 7


@dataclass(frozen=True)
class ScreenshotResult:
    ok: bool
    path: Optional[Path]
    message: str
    storage_root: Path


@dataclass(frozen=True)
class OCRResult:
    ok: bool
    text: str
    path: Path
    language: str
    message: str


@dataclass(frozen=True)
class LocateResult:
    ok: bool
    point: Optional[dict]
    bbox: Optional[dict]
    confidence: float
    reason: str

    @classmethod
    def fail(
        cls,
        reason: str,
        *,
        confidence: float = 0.0,
        point=None,
        bbox=None,
    ) -> "LocateResult":
        return cls(ok=False, point=point, bbox=bbox, confidence=confidence, reason=reason)
