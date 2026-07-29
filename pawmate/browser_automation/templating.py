"""Small, non-executable template and condition language for workflows."""

from __future__ import annotations

import json
import re
from typing import Any


TEMPLATE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
MAX_EXPANDED_STRING = 64 * 1024


class TemplateResolutionError(ValueError):
    pass


def lookup_path(context: dict[str, Any], path: str) -> Any:
    current: Any = context
    for part in path.strip().split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        raise TemplateResolutionError(f"Unknown workflow variable: {path}")
    return current


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_value(item, context) for item in value]
    if not isinstance(value, str):
        return value
    matches = list(TEMPLATE_RE.finditer(value))
    if not matches:
        return value
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return lookup_path(context, matches[0].group(1))

    def replacement(match: re.Match[str]) -> str:
        resolved = lookup_path(context, match.group(1))
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False, separators=(",", ":"))
        if resolved is None:
            return ""
        return str(resolved)

    expanded = TEMPLATE_RE.sub(replacement, value)
    if len(expanded.encode("utf-8")) > MAX_EXPANDED_STRING:
        raise TemplateResolutionError("Expanded template exceeds 65536 bytes")
    return expanded


def evaluate_condition(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    left = resolve_value(condition.get("left"), context)
    op = str(condition.get("op") or "truthy")
    if op == "exists":
        return left is not None
    if op == "truthy":
        return bool(left)
    if op == "falsy":
        return not bool(left)
    right = resolve_value(condition.get("right"), context)
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "contains":
        return _contains(left, right)
    if op == "not_contains":
        return not _contains(left, right)
    if op in {"gt", "gte", "lt", "lte"}:
        try:
            if op == "gt":
                return left > right
            if op == "gte":
                return left >= right
            if op == "lt":
                return left < right
            return left <= right
        except TypeError as exc:
            raise TemplateResolutionError(f"Condition values are not comparable: {exc}") from exc
    raise TemplateResolutionError(f"Unsupported condition operator: {op}")


def _contains(container: Any, item: Any) -> bool:
    try:
        return item in container
    except TypeError:
        return str(item) in str(container)
