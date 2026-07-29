"""Application log file helpers."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from pawmate.core.safety.redaction import redact_text
from pawmate.core.safety.resource_limits import MAX_LOG_TAIL_BYTES, read_tail_text
from pawmate.storage.app_paths import get_app_paths


LOG_FILE_NAMES = {
    "app": "pawmate.log",
    "operations": "operations.log",
    "gateway": "gateway.log",
}
LOG_KIND_ALIASES = {
    "all": "app",
    "operation": "operations",
    "ops": "operations",
    "gw": "gateway",
}


def normalize_log_kind(kind: str | None = None) -> str:
    raw = str(kind or "app").strip().lower()
    raw = LOG_KIND_ALIASES.get(raw, raw)
    return raw if raw in LOG_FILE_NAMES else "app"


def get_log_path(config: Optional[dict] = None, kind: str | None = None) -> Path:
    paths = get_app_paths(config)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    return paths.logs_dir / LOG_FILE_NAMES[normalize_log_kind(kind)]


def read_recent_logs(limit: int = 300, path: Optional[Path] = None, kind: str | None = None) -> dict:
    log_kind = normalize_log_kind(kind)
    log_path = path or get_log_path(kind=log_kind)
    limit = max(1, min(int(limit or 300), 2000))
    if not log_path.exists():
        return {
            "kind": log_kind,
            "path": str(log_path),
            "exists": False,
            "size": 0,
            "modified_at": None,
            "lines": [],
            "text": "",
        }

    limited = read_tail_text(log_path, encoding="utf-8", errors="replace", max_bytes=MAX_LOG_TAIL_BYTES)
    text = redact_text(limited.text)
    lines = text.splitlines()[-limit:]
    stat = log_path.stat()
    return {
        "kind": log_kind,
        "path": str(log_path),
        "exists": True,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
        "modified_label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "truncated": limited.truncated,
        "lines": lines,
        "text": "\n".join(lines),
    }


def clear_logs(path: Optional[Path] = None, kind: str | None = None) -> dict:
    log_kind = normalize_log_kind(kind)
    log_path = path or get_log_path(kind=log_kind)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    return read_recent_logs(path=log_path, kind=log_kind)
