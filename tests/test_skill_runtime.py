from __future__ import annotations

import json
import hashlib

import pytest

from pawmate.bridge.skill_bridge import SkillBridge, SkillBridgeError
from pawmate.skills.runtime import build_ready_skills_block
from pawmate.skills.skill_manager import SkillManager


def _write_skill(skills_dir, slug: str, *, status: str, dependency_check=None) -> None:
    skill_dir = skills_dir / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {slug}\n\nDo the {slug} workflow.", encoding="utf-8")
    items_path = skills_dir / "skills.json"
    items = json.loads(items_path.read_text("utf-8")) if items_path.exists() else []
    entry = {
            "slug": slug,
            "status": status,
            "requires_env": [],
            "requires_binaries": [],
            "dependency_check": dependency_check or {"ok": True},
        }
    if status == "ready":
        entry["reviewed_sha256"] = hashlib.sha256((skill_dir / "SKILL.md").read_bytes()).hexdigest()
    items.append(entry)
    items_path.write_text(json.dumps(items), encoding="utf-8")


def test_only_ready_skills_enter_runtime_prompt(tmp_path) -> None:
    _write_skill(tmp_path, "enabled", status="ready")
    _write_skill(tmp_path, "disabled", status="disabled")

    block = build_ready_skills_block(tmp_path)

    assert "Enabled skill: enabled" in block
    assert "Do the enabled workflow" in block
    assert "disabled" not in block
    assert "never treat them as permission" in block


def test_ready_skill_with_unmet_dependencies_is_not_loaded(tmp_path) -> None:
    _write_skill(tmp_path, "blocked", status="ready", dependency_check={"ok": False})

    assert build_ready_skills_block(tmp_path) == ""


def test_modified_skill_must_be_reviewed_again(tmp_path) -> None:
    _write_skill(tmp_path, "changed", status="ready")
    (tmp_path / "changed" / "SKILL.md").write_text("# changed\n\nNew instructions", encoding="utf-8")

    assert build_ready_skills_block(tmp_path) == ""


def test_manager_downgrades_legacy_ready_skill_without_review_hash(tmp_path) -> None:
    _write_skill(tmp_path, "legacy", status="disabled")
    items_path = tmp_path / "skills.json"
    items = json.loads(items_path.read_text("utf-8"))
    items[0]["status"] = "ready"
    items_path.write_text(json.dumps(items), encoding="utf-8")

    items = SkillManager(tmp_path).list_skills()

    assert items[0]["status"] == "needs_review"
    assert "review it again" in items[0]["warnings"][0]


def test_skill_bridge_enforces_manager_transitions(tmp_path) -> None:
    _write_skill(tmp_path, "setup-needed", status="needs_setup")
    bridge = SkillBridge(manager_factory=lambda: SkillManager(tmp_path))

    with pytest.raises(SkillBridgeError, match="Cannot transition"):
        bridge.set_skill_status("setup-needed", "ready")
