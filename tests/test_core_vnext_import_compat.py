import importlib
from pathlib import Path


ROOT = Path("pawmate/core")

REMOVED_ROOT_SHIMS = [
    "app_logs.py",
    "browser_diagnostics.py",
    "chat_service.py",
    "checkpoint.py",
    "confirm_gate.py",
    "engine.py",
    "environment_diagnostics.py",
    "file_boundary.py",
    "heartbeat.py",
    "language_mode.py",
    "llm_factory.py",
    "llm_provider.py",
    "llm_router.py",
    "loop_control.py",
    "model_catalog.py",
    "network_boundary.py",
    "path_security.py",
    "plugin_boundary.py",
    "prompt_assembler.py",
    "prompt_sections.py",
    "provider_contract.py",
    "provider_runner.py",
    "redaction.py",
    "resource_limits.py",
    "runtime_config.py",
    "runtime_paths.py",
    "runtime_state.py",
    "sandbox.py",
    "security_service.py",
    "shell_boundary.py",
    "task_planner.py",
    "tool_call_lifecycle.py",
    "tool_call_runner.py",
    "tool_observation.py",
    "tool_parser.py",
    "tool_policy.py",
    "tool_result_budget.py",
    "tool_router.py",
    "trace.py",
    "turn_context_builder.py",
]

NEW_MODULES = [
    "pawmate.core.runtime.engine",
    "pawmate.core.runtime.loop_control",
    "pawmate.core.runtime.runtime_config",
    "pawmate.core.runtime.runtime_paths",
    "pawmate.core.runtime.runtime_state",
    "pawmate.core.runtime.turn_context_builder",
    "pawmate.core.model.llm_factory",
    "pawmate.core.model.llm_provider",
    "pawmate.core.model.llm_router",
    "pawmate.core.model.model_catalog",
    "pawmate.core.model.provider_contract",
    "pawmate.core.model.provider_runner",
    "pawmate.core.model.providers.deepseek_provider",
    "pawmate.core.prompts.language_mode",
    "pawmate.core.prompts.prompt_assembler",
    "pawmate.core.prompts.prompt_sections",
    "pawmate.core.tools.tool_call_lifecycle",
    "pawmate.core.tools.tool_call_runner",
    "pawmate.core.tools.tool_observation",
    "pawmate.core.tools.tool_parser",
    "pawmate.core.tools.tool_policy",
    "pawmate.core.tools.tool_result_budget",
    "pawmate.core.tools.tool_router",
    "pawmate.core.safety.confirm_gate",
    "pawmate.core.safety.file_boundary",
    "pawmate.core.safety.network_boundary",
    "pawmate.core.safety.path_security",
    "pawmate.core.safety.plugin_boundary",
    "pawmate.core.safety.redaction",
    "pawmate.core.safety.resource_limits",
    "pawmate.core.safety.sandbox",
    "pawmate.core.safety.security_service",
    "pawmate.core.safety.shell_boundary",
    "pawmate.core.planning.task_planner",
    "pawmate.core.checkpoint.checkpoint",
    "pawmate.core.observability.app_logs",
    "pawmate.core.observability.browser_diagnostics",
    "pawmate.core.observability.environment_diagnostics",
    "pawmate.core.observability.trace",
    "pawmate.core.services.chat_service",
    "pawmate.core.services.heartbeat",
]

COMPAT_MODULES = [
    "pawmate.core.compat.engine",
    "pawmate.core.compat.provider_runner",
    "pawmate.core.compat.prompt_assembler",
    "pawmate.core.compat.security_service",
    "pawmate.core.compat.tool_router",
    "pawmate.core.compat.checkpoint",
]


def test_core_root_has_no_legacy_shim_files():
    remaining = [name for name in REMOVED_ROOT_SHIMS if (ROOT / name).exists()]

    assert remaining == []
    assert not (ROOT / "providers").exists()


def test_core_hard_migration_imports_new_modules():
    for name in NEW_MODULES:
        importlib.import_module(name)


def test_core_compat_imports_remain_available():
    for name in COMPAT_MODULES:
        importlib.import_module(name)


def test_core_vnext_modules_keep_app_config_path():
    from pawmate.core.model import llm_factory
    from pawmate.core.runtime import runtime_config

    expected = Path("pawmate/config.json").resolve()

    assert llm_factory.CONFIG_PATH.resolve() == expected
    assert runtime_config.CONFIG_PATH.resolve() == expected
