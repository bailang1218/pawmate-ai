"""
Skill manager — high-level skill management operations.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pawmate.skills.skill_sources import VALID_STATUSES, is_valid_slug, is_valid_status
from pawmate.skills.skill_store import (
    load_installed_skills, save_installed_skills, register_skill,
    update_skill_status, get_skill_status, list_local_skills,
    is_installed, write_origin_json, get_default_skills_dir,
)

_logger = logging.getLogger("pawmate")


class SkillManager:
    """High-level skill management: list, detail, review, uninstall, update."""

    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_NEEDS_SETUP = "needs_setup"
    STATUS_DISABLED = "disabled"
    STATUS_READY = "ready"
    STATUS_QUARANTINED = "quarantined"

    VALID_TRANSITIONS = {
        "needs_review": {"ready", "disabled", "quarantined"},
        "needs_setup": {"needs_review", "disabled", "quarantined"},
        "disabled": {"ready", "needs_review"},
        "ready": {"disabled"},
        "quarantined": {"needs_review", "disabled"},
    }

    def __init__(self, skills_dir: Optional[Path] = None):
        self._skills_dir = skills_dir or get_default_skills_dir()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_skills(self) -> List[Dict[str, Any]]:
        return list_local_skills(skills_dir=self._skills_dir)

    def get_detail(self, slug: str) -> Optional[Dict[str, Any]]:
        for s in self.list_skills():
            if s.get("slug") == slug:
                return s
        return None

    def read_skill_md(self, slug: str) -> Optional[str]:
        valid, err = is_valid_slug(slug)
        if not valid:
            return None
        path = self._skills_dir / slug / "SKILL.md"
        if not path.exists():
            return None
        return path.read_text("utf-8", errors="replace")

    def list_builtin_skills(self) -> List[Dict[str, Any]]:
        """List built-in PawMate skill templates."""
        builtin_dir = Path(__file__).resolve().parent / "builtin"
        if not builtin_dir.is_dir():
            return []
        result = []
        for d in sorted(builtin_dir.iterdir()):
            if not d.is_dir():
                continue
            md = d / "SKILL.md"
            if not md.exists():
                continue
            slug = d.name
            text = md.read_text("utf-8", errors="replace")
            name = slug
            desc = ""
            for line in text.split("\n"):
                if line.startswith("# "):
                    name = line[2:].strip()
                elif line.startswith("## ") and "summary" in line.lower():
                    pass
                elif line and not desc and not line.startswith("#"):
                    desc = line.strip()[:200]
                    break
            result.append({
                "slug": slug,
                "name": name,
                "description": desc,
                "source": "builtin",
                "installed": is_installed(slug, skills_dir=self._skills_dir),
            })
        return result

    def install_builtin(self, slug: str) -> Dict[str, Any]:
        """Install a built-in skill template to data/skills."""
        builtin_dir = Path(__file__).resolve().parent / "builtin" / slug
        if not builtin_dir.is_dir():
            raise ValueError(f"Built-in skill '{slug}' not found")
        if not (builtin_dir / "SKILL.md").exists():
            raise ValueError(f"Built-in skill '{slug}' missing SKILL.md")
        if is_installed(slug, skills_dir=self._skills_dir):
            raise ValueError(f"Skill '{slug}' is already installed")

        target = self._skills_dir / slug
        shutil.copytree(builtin_dir, target, ignore=shutil.ignore_patterns(
            "__pycache__", ".git", "node_modules", ".venv", "dist", "build"
        ))

        origin = {
            "slug": slug, "version": "1.0.0",
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "source": "builtin",
        }
        write_origin_json(slug, origin, skills_dir=self._skills_dir)
        meta = {
            "slug": slug, "name": slug, "version": "1.0.0",
            "owner": "PawMate", "summary": "Built-in skill",
            "source": "builtin",
            "requires": {"env": [], "binaries": []},
        }
        return register_skill(meta, skills_dir=self._skills_dir)

    # ------------------------------------------------------------------
    # Status operations
    # ------------------------------------------------------------------

    def set_status(self, slug: str, new_status: str) -> Dict[str, Any]:
        """Set skill status with transition validation."""
        if not is_valid_status(new_status):
            raise ValueError(f"Invalid status: {new_status!r}")
        current = get_skill_status(slug, skills_dir=self._skills_dir)
        if current is None:
            raise ValueError(f"Skill '{slug}' not found")
        allowed = self.VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ValueError(
                f"Cannot transition from '{current}' to '{new_status}'. "
                f"Allowed: {sorted(allowed)}"
            )
        result = update_skill_status(slug, new_status, skills_dir=self._skills_dir)
        if result is None:
            raise ValueError(f"Skill '{slug}' not found")
        return result

    def review_skill(self, slug: str, approved: bool, notes: str = "") -> Dict[str, Any]:
        """Review a skill: approve → ready, or keep needs_review."""
        current = get_skill_status(slug, skills_dir=self._skills_dir)
        if current is None:
            raise ValueError(f"Skill '{slug}' not found")
        if current == "quarantined":
            raise ValueError("Quarantined skills require manual review before approval")
        if approved:
            return self.set_status(slug, self.STATUS_READY)
        # Keep needs_review, just record notes
        return self.get_detail(slug) or {}

    # ------------------------------------------------------------------
    # Uninstall
    # ------------------------------------------------------------------

    def uninstall(self, slug: str, keep_backup: bool = True) -> Dict[str, Any]:
        """Uninstall a skill. Moves to _backup if keep_backup=True."""
        valid, err = is_valid_slug(slug)
        if not valid:
            raise ValueError(err)
        skill_dir = self._skills_dir / slug
        if not skill_dir.is_dir():
            raise ValueError(f"Skill '{slug}' not found at {skill_dir}")

        if keep_backup:
            backup_dir = self._skills_dir / "_backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_target = backup_dir / f"{slug}-{ts}"
            shutil.move(str(skill_dir), str(backup_target))
            _logger.info("[SkillManager] Backed up %s -> %s", slug, backup_target)
        else:
            shutil.rmtree(skill_dir)

        # Remove from skills.json
        items = load_installed_skills(self._skills_dir / "skills.json")
        items = [i for i in items if i.get("slug") != slug]
        save_installed_skills(items, self._skills_dir / "skills.json")

        _logger.info("[SkillManager] Uninstalled %s", slug)
        return {"slug": slug, "action": "uninstalled", "backed_up": keep_backup}
