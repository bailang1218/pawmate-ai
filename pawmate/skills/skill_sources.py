"""Skill sources — source type definitions for multi-origin skill management."""
from __future__ import annotations

from enum import Enum


class SkillSource(str, Enum):
    builtin = "builtin"
    local = "local"
    clawhub = "clawhub"
    url = "url"
    git = "git"


VALID_STATUSES = {
    "needs_review",
    "needs_setup",
    "disabled",
    "ready",
    "quarantined",
}


def is_valid_status(status: str) -> bool:
    return status in VALID_STATUSES


def is_valid_slug(slug: str) -> tuple[bool, str]:
    """Validate skill slug — no path traversal, no special chars."""
    if not slug:
        return False, "slug is empty"
    if len(slug) > 80:
        return False, f"slug too long ({len(slug)} > 80)"
    if ".." in slug:
        return False, "path traversal blocked"
    if "/" in slug or "\\" in slug:
        return False, "path separators not allowed"
    if ":" in slug:
        return False, "drive letters not allowed"
    # Only allow a-z, A-Z, 0-9, _, -, .
    for ch in slug:
        if ch.isalnum() or ch in "_-.":
            continue
        return False, f"invalid character: {ch!r}"
    return True, ""
