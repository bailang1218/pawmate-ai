# Plugin Boundary

## Responsibility

Plugins must enter runtime through a manifest and ToolRegistry. They cannot
directly edit system prompts, bypass policy, or expose tools to the model
without explicit visibility.

## Inputs

- Plugin manifest object.
- Plugin tool specifications.
- Plugin handlers supplied by the loader.

## Outputs

- Validated `PluginManifest`.
- Validated `PluginToolSpec`.
- `ToolDef` registered with source, permissions, risk, side effect, timeout,
  and model visibility metadata.

## Invariants

- Manifest requires name, version, and relative entrypoint.
- Prompt/runtime policy fields are forbidden.
- Plugin tools require object JSON schema.
- Plugin tools default `model_visible=false`.
- High-risk or destructive plugin tools are forced to confirm approval.
- Plugin output is wrapped as untrusted data-only observation.

## Forbidden

- Plugin path traversal in entrypoint.
- Plugin bypassing ToolRegistry schema validation.
- Plugin output entering system prompt.
- Plugin tool bypassing policy or ConfirmGate.

## Tests

- `tests/test_tool_registry_plugin_boundary.py`

## Code Paths

- `pawmate/core/plugin_boundary.py`
- `pawmate/tools/core/registry.py`
- `pawmate/core/tools/tool_observation.py`
