"""Compatibility shim for existing imports.

The implementation lives in `pawmate.ocr_vision`.
"""

from pawmate.ocr_vision.service import capture_screenshot, ocr_image, vision_analyze  # noqa: F401

__all__ = ["capture_screenshot", "ocr_image", "vision_analyze"]
