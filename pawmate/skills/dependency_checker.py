"""Local dependency checks for PawMate skills.

The checker is intentionally passive: it reports missing environment variables,
missing binaries, and declared manual install steps. It never installs or runs
anything on behalf of the user.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return []


def check_skill_dependencies(requires: Dict[str, Any] | None) -> Dict[str, Any]:
    req = requires if isinstance(requires, dict) else {}
    env = _as_str_list(req.get("env"))
    binaries = _as_str_list(req.get("binaries"))
    install = _as_str_list(req.get("install"))

    present_env = [name for name in env if os.environ.get(name)]
    missing_env = [name for name in env if not os.environ.get(name)]
    present_binaries = [name for name in binaries if shutil.which(name)]
    missing_binaries = [name for name in binaries if not shutil.which(name)]

    warnings: list[str] = []
    if missing_env:
        warnings.append("Missing required environment variables: " + ", ".join(missing_env))
    if missing_binaries:
        warnings.append("Missing required binaries: " + ", ".join(missing_binaries))
    if install:
        warnings.append("Manual install steps declared; PawMate will not run them automatically.")

    return {
        "checked": True,
        "ok": not missing_env and not missing_binaries,
        "env": {"present": present_env, "missing": missing_env},
        "binaries": {"present": present_binaries, "missing": missing_binaries},
        "install": install,
        "warnings": warnings,
    }
