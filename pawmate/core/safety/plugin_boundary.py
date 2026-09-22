"""Plugin manifest and tool registration boundary."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable

from pawmate.tools.core.registry import (
    APPROVAL_AUTO,
    APPROVAL_CONFIRM,
    RiskLevel,
    SideEffectLevel,
    ToolCategory,
    ToolDef,
    ToolRegistry,
)


FORBIDDEN_MANIFEST_FIELDS = {
    "system_prompt",
    "developer_prompt",
    "prompt",
    "runtime_policy",
}


class PluginBoundaryError(ValueError):
    pass


@dataclass(frozen=True)
class PluginToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    permissions: list[str] = field(default_factory=list)
    category: str = ToolCategory.GENERAL.value
    risk: str = RiskLevel.LOW.value
    side_effect: str = SideEffectLevel.READ_ONLY.value
    timeout: float = 60.0
    model_visible: bool = False
    approval: str = APPROVAL_AUTO


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    entrypoint: str
    permissions: list[str] = field(default_factory=list)
    sandbox: str = "isolated"
    model_visible: bool = False
    risk: str = RiskLevel.LOW.value
    source: str = "plugin"
    tools: list[PluginToolSpec] = field(default_factory=list)


def parse_plugin_manifest(payload: dict[str, Any]) -> PluginManifest:
    if not isinstance(payload, dict):
        raise PluginBoundaryError("plugin manifest must be an object")
    forbidden = sorted(FORBIDDEN_MANIFEST_FIELDS.intersection(payload.keys()))
    if forbidden:
        raise PluginBoundaryError(f"plugin manifest cannot define prompt/runtime fields: {', '.join(forbidden)}")

    name = _required_string(payload, "name")
    version = _required_string(payload, "version")
    entrypoint = _required_string(payload, "entrypoint")
    _validate_entrypoint(entrypoint)

    permissions = _string_list(payload.get("permissions", []), "permissions")
    tools_payload = payload.get("tools", [])
    if not isinstance(tools_payload, list):
        raise PluginBoundaryError("plugin manifest tools must be a list")

    default_model_visible = bool(payload.get("model_visible", False))
    default_risk = str(payload.get("risk") or RiskLevel.LOW.value)
    tools = [
        _parse_plugin_tool(
            item,
            default_permissions=permissions,
            default_model_visible=default_model_visible,
            default_risk=default_risk,
        )
        for item in tools_payload
    ]
    return PluginManifest(
        name=name,
        version=version,
        entrypoint=entrypoint,
        permissions=permissions,
        sandbox=str(payload.get("sandbox") or "isolated"),
        model_visible=default_model_visible,
        risk=default_risk,
        source=str(payload.get("source") or "plugin"),
        tools=tools,
    )


def register_plugin_tool(
    registry: ToolRegistry,
    manifest: PluginManifest,
    tool_spec: PluginToolSpec,
    handler: Callable[..., Any],
) -> ToolDef:
    if tool_spec.model_visible and manifest.sandbox != "trusted_in_process":
        raise PluginBoundaryError(
            "model-visible plugin tools require sandbox='trusted_in_process'; "
            "isolated plugin execution is not implemented and fails closed"
        )
    tool_def = plugin_tool_to_tool_def(manifest, tool_spec, handler)
    registry.register(tool_def)
    return tool_def


def plugin_tool_to_tool_def(
    manifest: PluginManifest,
    tool_spec: PluginToolSpec,
    handler: Callable[..., Any],
) -> ToolDef:
    permissions = sorted(set(manifest.permissions + tool_spec.permissions))
    approval = tool_spec.approval
    high_risk = tool_spec.risk in {RiskLevel.HIGH.value, RiskLevel.CRITICAL.value}
    side_effect_risk = tool_spec.side_effect in {
        SideEffectLevel.EXTERNAL_WRITE.value,
        SideEffectLevel.DESTRUCTIVE.value,
        SideEffectLevel.PRIVILEGED.value,
    }
    if high_risk or side_effect_risk:
        approval = APPROVAL_CONFIRM
    return ToolDef(
        name=tool_spec.name,
        description=tool_spec.description,
        input_schema=tool_spec.input_schema,
        handler=handler,
        category=tool_spec.category,
        risk=tool_spec.risk,
        side_effect=tool_spec.side_effect,
        timeout=tool_spec.timeout,
        model_visible=tool_spec.model_visible,
        approval=approval,
        requires_confirm=approval == APPROVAL_CONFIRM,
        permissions=permissions,
        tags=["plugin", manifest.name],
        source=f"plugin:{manifest.name}@{manifest.version}",
    )


def _parse_plugin_tool(
    payload: Any,
    *,
    default_permissions: list[str],
    default_model_visible: bool,
    default_risk: str,
) -> PluginToolSpec:
    if not isinstance(payload, dict):
        raise PluginBoundaryError("plugin tool definition must be an object")
    forbidden = sorted(FORBIDDEN_MANIFEST_FIELDS.intersection(payload.keys()))
    if forbidden:
        raise PluginBoundaryError(f"plugin tool cannot define prompt/runtime fields: {', '.join(forbidden)}")

    return PluginToolSpec(
        name=_required_string(payload, "name"),
        description=_required_string(payload, "description"),
        input_schema=_required_schema(payload.get("input_schema")),
        permissions=_string_list(payload.get("permissions", default_permissions), "tool.permissions"),
        category=str(payload.get("category") or ToolCategory.GENERAL.value),
        risk=str(payload.get("risk") or default_risk),
        side_effect=str(payload.get("side_effect") or SideEffectLevel.READ_ONLY.value),
        timeout=float(payload.get("timeout") or 60.0),
        model_visible=bool(payload.get("model_visible", default_model_visible)),
        approval=str(payload.get("approval") or APPROVAL_AUTO),
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise PluginBoundaryError(f"plugin manifest requires {key}")
    return value


def _required_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") != "object":
        raise PluginBoundaryError("plugin tool requires object input_schema")
    return value


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginBoundaryError(f"{field_name} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _validate_entrypoint(entrypoint: str) -> None:
    path = PurePosixPath(entrypoint.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise PluginBoundaryError("plugin entrypoint must be relative and cannot traverse directories")
