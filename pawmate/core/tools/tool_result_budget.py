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
import threading
from dataclasses import dataclass
from typing import Any

from pawmate.core.safety.redaction import redact_for_json, redact_text
from pawmate.storage.app_paths import get_app_paths


UI_RESULT_CHARS = 800
MODEL_RESULT_CHARS = 6000
FRAME_MODEL_RESULT_CHARS = 2200
LOG_RESULT_CHARS = 1200
RAW_RESULT_MAX_FILES = 500
RAW_RESULT_MAX_BYTES = 50 * 1024 * 1024
_RESULT_STORE_LOCK = threading.Lock()


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
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > RAW_RESULT_MAX_BYTES:
        encoded = encoded[:RAW_RESULT_MAX_BYTES]
    with _RESULT_STORE_LOCK:
        _prune_result_store(base, incoming_bytes=len(encoded))
        path.write_bytes(encoded)
    return str(path)


def _prune_result_store(base, *, incoming_bytes: int) -> None:
    files = []
    total = 0
    for item in base.glob("*.txt"):
        try:
            stat = item.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, stat.st_size, item))
        total += stat.st_size
    files.sort()
    while files and (
        len(files) >= RAW_RESULT_MAX_FILES
        or total + incoming_bytes > RAW_RESULT_MAX_BYTES
    ):
        _, size, oldest = files.pop(0)
        try:
            oldest.unlink()
            total -= size
        except OSError:
            continue


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


def _ui_projection_for_tool(tool_name: str, full: str) -> str:
    name = str(tool_name or "")
    if name == "native_web_search":
        return _native_web_search_ui_projection(full)
    if name in {"search_memory", "search_history", "open_history_context"}:
        if "No matching" in full or "No source messages" in full or "没有" in full:
            return "没有检索到相关记忆。"
        match = re.search(r"Found\s+(\d+)\s+data-only", full, re.IGNORECASE)
        count = match.group(1) if match else ""
        if count:
            return f"已检索到 {count} 条相关记忆；内容仅作为模型上下文使用，不在工具详情展示。"
        return "已检索相关记忆；内容仅作为模型上下文使用，不在工具详情展示。"
    if "[DATA_ONLY_TOOL_OBSERVATION]" in full or "data_only=true" in full:
        return "工具返回了 data-only 上下文；内容仅供模型推理使用，不在工具详情展示。"
    return full


def _native_web_search_ui_projection(full: str) -> str:
    try:
        data = json.loads(full)
    except Exception:
        return full
    if not isinstance(data, dict):
        return full
    sources = []
    for source in data.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sources.append(
            {
                "title": str(source.get("title") or "")[:120],
                "url": str(source.get("url") or "")[:120],
                "snippet": str(source.get("snippet") or "")[:60],
            }
        )
        if len(sources) >= 2:
            break
    attempts = []
    for item in data.get("attempts") or []:
        if not isinstance(item, dict):
            continue
        attempts.append(
            {
                "provider": item.get("provider"),
                "status": item.get("status"),
                "error_type": item.get("error_type"),
                "message": str(item.get("message") or "")[:90],
            }
        )
        if len(attempts) >= 3:
            break
    compact = {
        "ok": data.get("ok"),
        "operation": "native_web_search",
        "provider": data.get("provider"),
        "model": data.get("model"),
        "grounded": data.get("grounded"),
        "evidence_count": data.get("evidence_count"),
        "citation_required": data.get("citation_required"),
        "query": str(data.get("query") or "")[:160],
        "answer": str(data.get("answer") or data.get("message") or "")[:240],
        "sources": sources,
        "attempts": attempts,
        "error_type": data.get("error_type"),
    }
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= UI_RESULT_CHARS:
        return encoded

    compact["answer"] = str(compact.get("answer") or "")[:120]
    for source in compact["sources"]:
        source.pop("snippet", None)
    for attempt in compact["attempts"]:
        attempt["message"] = str(attempt.get("message") or "")[:40]
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= UI_RESULT_CHARS:
        return encoded

    compact["sources"] = compact["sources"][:1]
    compact["attempts"] = compact["attempts"][:1]
    compact["answer"] = str(compact.get("answer") or "")[:80]
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def budget_tool_result(tool_name: str, result: Any, session_id: str = "") -> ToolResultViews:
    full = _project_redacted_text(result)
    original_chars = len(full)
    ui_full = _ui_projection_for_tool(tool_name, full)
    model_limit = FRAME_MODEL_RESULT_CHARS if _is_browser_or_vision_tool(tool_name) else MODEL_RESULT_CHARS
    needs_file = original_chars > max(UI_RESULT_CHARS, model_limit, LOG_RESULT_CHARS)
    raw_ref = _write_full_result(tool_name, full, session_id=session_id) if needs_file else None

    ref_suffix = ""
    if raw_ref:
        ref_suffix = (
            f"\n\n[tool_result_truncated: full redacted result saved to {raw_ref}; "
            f"{original_chars} chars total]"
        )

    ui, ui_truncated = _truncate(ui_full, UI_RESULT_CHARS, ref_suffix)
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
