"""
Skill manifest — parse SKILL.md, frontmatter, and requires metadata.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger("pawmate")


def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, Any]], str, List[str]]:
    """Parse YAML frontmatter from a SKILL.md text.

    Returns (frontmatter_dict, body_text, warnings).
    frontmatter_dict is None if no frontmatter is found.
    """
    warnings: List[str] = []

    if not text.startswith("---"):
        return None, text, warnings

    end = text.find("---", 3)
    if end < 0:
        warnings.append("SKILL.md has opening --- but no closing ---")
        return None, text, warnings

    raw = text[3:end].strip()
    body = text[end + 3:].strip()

    if not raw:
        return {}, body, warnings

    try:
        import yaml
        try:
            fm = yaml.safe_load(raw)
            if not isinstance(fm, dict):
                warnings.append(f"Frontmatter is not a dict: {type(fm).__name__}")
                return None, body, warnings
            return fm, body, warnings
        except yaml.YAMLError as e:
            warnings.append(f"YAML parse error: {e}")
            return None, body, warnings
    except ImportError:
        # No PyYAML — use minimal frontmatter parser
        fm = _minimal_yaml_parse(raw)
        if fm is None:
            warnings.append("PyYAML not installed, using minimal frontmatter parser")
        return fm, body, warnings


def _minimal_yaml_parse(raw: str) -> Optional[Dict[str, Any]]:
    """Minimal YAML-like parser for simple frontmatter."""
    result: Dict[str, Any] = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([\w_]+)\s*:\s*(.*)$", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            # Remove surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result if result else None


def extract_requires(frontmatter: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract requirement information from frontmatter.

    Returns dict with keys:
      - env: list of required environment variable names
      - binaries: list of required binary/tool names
      - install: installation steps (if any)
    """
    result: Dict[str, Any] = {"env": [], "binaries": [], "install": []}

    if not frontmatter:
        return result

    # Field: requires.env or requires_env
    req = frontmatter.get("requires", {})
    if isinstance(req, dict):
        result["env"] = req.get("env") or frontmatter.get("requires_env") or []
    elif isinstance(req, list):
        # "requires:" is just a list of strings — treat as env names
        result["env"] = req

    # Also check top-level requires_env
    top_env = frontmatter.get("requires_env")
    if top_env and isinstance(top_env, list):
        result["env"] = top_env

    # binaries
    top_bin = frontmatter.get("requires_binaries") or frontmatter.get("binaries")
    if top_bin and isinstance(top_bin, list):
        result["binaries"] = top_bin
    if isinstance(req, dict):
        bins = req.get("binaries") or req.get("binary") or []
        if isinstance(bins, list):
            result["binaries"] = bins

    # install steps
    install = frontmatter.get("install") or (req.get("install") if isinstance(req, dict) else None)
    if install and isinstance(install, list):
        result["install"] = install

    # Clean up types — ensure lists
    for key in ("env", "binaries", "install"):
        val = result[key]
        if isinstance(val, str):
            result[key] = [val]
        elif not isinstance(val, list):
            result[key] = []

    return result


def extract_summary(
    frontmatter: Optional[Dict[str, Any]],
    body: str,
) -> str:
    """Extract a human-readable summary from frontmatter or body."""
    if frontmatter:
        for key in ("description", "summary", "name"):
            val = frontmatter.get(key)
            if val and isinstance(val, str):
                return val.strip()
    # Fall back to first non-empty line of body
    for line in body.split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:200]
    return "(no description)"


def parse_skill_md(text: str) -> Dict[str, Any]:
    """Full parse of a SKILL.md string.

    Returns dict with keys:
      - frontmatter: dict or None
      - body: str
      - summary: str
      - requires: dict
      - warnings: list[str]
    """
    frontmatter, body, warnings = parse_frontmatter(text)
    requires = extract_requires(frontmatter)
    summary = extract_summary(frontmatter, body)

    return {
        "frontmatter": frontmatter,
        "body": body,
        "summary": summary,
        "requires": requires,
        "warnings": warnings,
    }
