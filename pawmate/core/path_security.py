"""Path access policy for local tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class PathSecurityError(Exception):
    """Raised when a path is outside the configured access policy."""


def normalize_allowed_roots(raw: Any) -> list[Path]:
    """Return resolved allowed roots, defaulting to the user's home."""
    if not raw:
        return [Path.home().resolve()]

    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, (list, tuple, set)):
        values = []

    roots: list[Path] = []
    for item in values:
        text = str(item).strip()
        if not text:
            continue
        try:
            roots.append(Path(text).expanduser().resolve())
        except Exception as exc:
            raise PathSecurityError(f"Invalid allowed root: {text}") from exc

    return roots or [Path.home().resolve()]


def is_path_under_any_root(path_obj: Path, roots: list[Path]) -> bool:
    """Return True when path_obj is inside one of roots."""
    for root in roots:
        try:
            path_obj.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def sanitize_path(path: str, security_config: dict[str, Any]) -> str:
    """Resolve and validate a path against normalized security config."""
    try:
        path_obj = Path(path).expanduser().resolve()
    except Exception as exc:
        raise PathSecurityError(f"Invalid path: {path}") from exc

    mode = str(
        security_config.get("path_access_mode")
        or security_config.get("mode")
        or "strict"
    ).strip().lower()

    if mode != "strict":
        return str(path_obj)

    allowed_roots = normalize_allowed_roots(
        security_config.get("allowed_roots")
        or security_config.get("allowed_path")
        or []
    )
    if not is_path_under_any_root(path_obj, allowed_roots):
        roots_text = ", ".join(str(root) for root in allowed_roots)
        raise PathSecurityError(
            f"Path '{path}' is outside the allowed roots: {roots_text}"
        )

    return str(path_obj)
