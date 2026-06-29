"""Runtime configuration helpers backed by pawmate/config.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load_runtime_config() -> dict[str, Any]:
    """Load the app runtime config from the same file used by Settings."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def normalize_security_config(sec: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize security config while keeping legacy keys available."""
    raw = sec if isinstance(sec, dict) else {}

    path_access_mode = (
        raw.get("path_access_mode")
        or raw.get("mode")
        or "strict"
    )
    allowed_roots = (
        raw.get("allowed_roots")
        or raw.get("allowed_path")
        or ["~"]
    )
    command_sandbox_mode = (
        raw.get("command_sandbox_mode")
        or raw.get("sandbox_mode")
        or "safe"
    )

    path_access_mode = str(path_access_mode).strip().lower()
    command_sandbox_mode = str(command_sandbox_mode).strip().lower()

    return {
        "path_access_mode": path_access_mode,
        "allowed_roots": allowed_roots,
        "command_sandbox_mode": command_sandbox_mode,
        "mode": path_access_mode,
        "allowed_path": allowed_roots,
        "sandbox_mode": command_sandbox_mode,
    }


def get_security_config() -> dict[str, Any]:
    """Return normalized security settings from pawmate/config.json."""
    cfg = load_runtime_config()
    return normalize_security_config(cfg.get("security", {}))
