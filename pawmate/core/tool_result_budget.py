"""Tiered budgets for tool results.

Tool output can be useful and dangerous at the same time: it may be huge,
expensive to feed back to the model, noisy in logs, and awkward in the UI.
This module keeps a redacted full copy on disk when needed and returns smaller
views for each consumer.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from pawmate.core.redaction import redact_for_json, redact_text
from pawmate.storage.app_paths import get_app_paths


UI_RESULT_CHARS = 800
MODEL_RESULT_CHARS = 6000
FRAME_MODEL_RESULT_CHARS = 2200
LOG_RESULT_CHARS = 1200


@dataclass(slots=True)
class ToolResultViews:
    ui: str
    model: str
    log: str
    raw_ref: str | None
    truncated: bool
    original_chars: int


def _truncate(text: str, limit: int, suffix: str = "") -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if len(suffix) >= limit:
        return suffix[:limit], True
    keep = max(0, limit - len(suffix))
    return text[:keep] + suffix, True


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())[:60]
    return cleaned or "tool"


def _write_full_result(tool_name: str, text: str, session_id: str = "") -> str:
    base = get_app_paths().data_root / "tool_results"
    base.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    sid = _safe_name(session_id) if session_id else "session"
    name = f"{stamp}-{sid}-{_safe_name(tool_name)}-{digest}.txt"
    path = base / name
    path.write_text(text, encoding="utf-8", errors="replace")
    return str(path)


def _project_redacted_text(value: Any) -> str:
    legacy_projection = getattr(value, "to_legacy_string", None)
    if callable(legacy_projection):
        projected = legacy_projection()
        return projected if isinstance(projected, str) else str(projected)
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_for_json(value)
    try:
        return redact_text(json.dumps(value, ensure_ascii=False))
    except TypeError:
        return redact_text(str(value))


def _is_browser_or_vision_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    return (
        name.startswith("browser_")
        or name.startswith("native_browser_")
        or name in {"capture_screenshot", "ocr_image", "vision_analyze"}
    )


def budget_tool_result(tool_name: str, result: Any, session_id: str = "") -> ToolResultViews:
    full = _project_redacted_text(result)
    original_chars = len(full)
    model_limit = FRAME_MODEL_RESULT_CHARS if _is_browser_or_vision_tool(tool_name) else MODEL_RESULT_CHARS
    needs_file = original_chars > max(UI_RESULT_CHARS, model_limit, LOG_RESULT_CHARS)
    raw_ref = _write_full_result(tool_name, full, session_id=session_id) if needs_file else None

    ref_suffix = ""
    if raw_ref:
        ref_suffix = (
            f"\n\n[tool_result_truncated: full redacted result saved to {raw_ref}; "
            f"{original_chars} chars total]"
        )

    ui, ui_truncated = _truncate(full, UI_RESULT_CHARS, ref_suffix)
    model, model_truncated = _truncate(full, model_limit, ref_suffix)
    log, log_truncated = _truncate(full, LOG_RESULT_CHARS, ref_suffix)

    return ToolResultViews(
        ui=ui,
        model=model,
        log=log,
        raw_ref=raw_ref,
        truncated=bool(raw_ref or ui_truncated or model_truncated or log_truncated),
        original_chars=original_chars,
    )
