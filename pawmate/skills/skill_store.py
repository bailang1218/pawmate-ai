"""
Local skill store — manage installed skill metadata and status.
All runtime data lives under <project_root>/data/skills/.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pawmate.skills.dependency_checker import check_skill_dependencies
from pawmate.storage.app_paths import AppPaths, get_app_paths

_logger = logging.getLogger("pawmate")

SKILLS_JSON = "skills.json"
ORIGIN_JSON = "origin.json"
ORIGIN_DIR = ".pawmate"

STATUS_NEEDS_REVIEW = "needs_review"
STATUS_NEEDS_SETUP = "needs_setup"
STATUS_DISABLED = "disabled"
STATUS_READY = "ready"
STATUS_QUARANTINED = "quarantined"
STATUS_QUARANTINED = "quarantined"

VALID_STATUSES = {STATUS_NEEDS_REVIEW, STATUS_NEEDS_SETUP, STATUS_DISABLED, STATUS_READY, STATUS_QUARANTINED}


def get_default_skills_dir() -> Path:
    """Return the default skills directory from AppPaths."""
    return get_app_paths().skills_dir


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _skills_json_path(base_dir: Optional[Path] = None) -> Path:
    return (base_dir or get_default_skills_dir()) / SKILLS_JSON


def _validate_slug(slug: str) -> None:
    """Validate a skill slug — no path traversal, no special chars."""
    import re
    if not slug or not re.match(r"^[a-zA-Z0-9_.-]+$", slug):
        raise ValueError(
            f"Invalid skill slug: {slug!r}. Only a-z, A-Z, 0-9, _, -, . allowed."
        )
    if ".." in slug:
        raise ValueError(f"Path traversal blocked in slug: {slug!r}")


# ── Read / write skills.json ─────────────────────────────────


def load_installed_skills(skills_json_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = skills_json_path or _skills_json_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        _logger.warning("[SkillStore] Failed to load skills.json: %s", e)
        return []


def save_installed_skills(items: List[Dict[str, Any]], skills_json_path: Optional[Path] = None) -> None:
    path = skills_json_path or _skills_json_path()
    _ensure_dir(path.parent)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def register_skill(metadata: Dict[str, Any], skills_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Register a newly installed skill. Returns the registered entry."""
    slug = metadata.get("slug", "")
    if not slug:
        raise ValueError("slug is required")
    _validate_slug(slug)

    skills_dir = skills_dir or get_default_skills_dir()
    sj_path = skills_dir / SKILLS_JSON
    items = load_installed_skills(sj_path)

    for item in items:
        if item.get("slug") == slug:
            raise ValueError(f"Skill '{slug}' is already installed")

    requires = metadata.get("requires", {}) or {}
    env_reqs: list = requires.get("env", []) if isinstance(requires, dict) else []
    bin_reqs: list = requires.get("binaries", []) if isinstance(requires, dict) else []
    dependency_check = metadata.get("dependency_check")
    if not isinstance(dependency_check, dict):
        dependency_check = check_skill_dependencies(requires if isinstance(requires, dict) else {})
    status = STATUS_NEEDS_REVIEW
    if not dependency_check.get("ok", True):
        status = STATUS_NEEDS_SETUP

    warnings = list(metadata.get("warnings", []) or [])
    for warning in dependency_check.get("warnings", []) or []:
        if warning not in warnings:
            warnings.append(warning)

    entry = {
        "slug": slug,
        "name": metadata.get("name") or metadata.get("displayName", slug),
        "version": metadata.get("version", "0.0.0"),
        "owner": metadata.get("owner", ""),
        "summary": metadata.get("summary", ""),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source": metadata.get("source", "clawhub"),
        "status": status,
        "requires_env": env_reqs,
        "requires_binaries": bin_reqs,
        "dependency_check": dependency_check,
        "local_path": str(skills_dir / slug),
        "origin_url": metadata.get("origin_url", ""),
        "download_url": metadata.get("download_url", ""),
        "warnings": warnings,
    }

    items.append(entry)
    save_installed_skills(items, sj_path)
    _logger.info("[SkillStore] Registered skill: %s (status=%s)", slug, status)
    return entry


def update_skill_status(slug: str, new_status: str, skills_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {new_status!r}")
    sj_path = (skills_dir or get_default_skills_dir()) / SKILLS_JSON
    items = load_installed_skills(sj_path)
    for item in items:
        if item.get("slug") == slug:
            item["status"] = new_status
            save_installed_skills(items, sj_path)
            _logger.info("[SkillStore] Updated %s status -> %s", slug, new_status)
            return item
    return None


def get_skill_status(slug: str, skills_dir: Optional[Path] = None) -> Optional[str]:
    sj_path = (skills_dir or get_default_skills_dir()) / SKILLS_JSON
    for item in load_installed_skills(sj_path):
        if item.get("slug") == slug:
            return item.get("status", STATUS_NEEDS_REVIEW)
    return None


def list_local_skills(skills_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    sj_path = (skills_dir or get_default_skills_dir()) / SKILLS_JSON
    return load_installed_skills(sj_path)


def is_installed(slug: str, skills_dir: Optional[Path] = None) -> bool:
    sj_path = (skills_dir or get_default_skills_dir()) / SKILLS_JSON
    return any(item.get("slug") == slug for item in load_installed_skills(sj_path))


def write_origin_json(slug: str, origin: Dict[str, Any], skills_dir: Optional[Path] = None) -> None:
    skills_dir = skills_dir or get_default_skills_dir()
    origin_dir = skills_dir / slug / ORIGIN_DIR
    _ensure_dir(origin_dir)
    path = origin_dir / ORIGIN_JSON
    with open(path, "w", encoding="utf-8") as f:
        json.dump(origin, f, indent=2, ensure_ascii=False)
    _logger.info("[SkillStore] Wrote origin.json for %s", slug)
