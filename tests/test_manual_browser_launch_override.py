import pytest

from pawmate.core.prompts.prompt_assembler import build_runtime_prompt
from pawmate.core.runtime.engine import AgentEngine
from pawmate.core.runtime.runtime_config import normalize_security_config
from pawmate.core.tools.tool_router import route_tools_for_turn
from pawmate.core.safety.shell_boundary import (
    ShellBoundaryError,
    analyze_shell_command,
    browser_process_launch_allowed,
    configure_shell_boundary,
)


@pytest.fixture(autouse=True)
def reset_browser_process_launch_override():
    configure_shell_boundary(allow_browser_process_launch=False)
    yield
    configure_shell_boundary(allow_browser_process_launch=False)


def test_browser_start_process_is_blocked_by_default():
    with pytest.raises(ShellBoundaryError, match="background or detached"):
        analyze_shell_command("Start-Process msedge.exe")


def test_manual_override_allows_only_a_single_supported_browser_launch():
    configure_shell_boundary(allow_browser_process_launch=True)

    boundary = analyze_shell_command(
        'Start-Process -FilePath "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" '
        "-ArgumentList '--remote-debugging-port=9222'"
    )

    assert boundary.background is True
    with pytest.raises(ShellBoundaryError, match="background or detached"):
        analyze_shell_command("Start-Process notepad.exe")
    with pytest.raises(ShellBoundaryError, match="background or detached"):
        analyze_shell_command("Start-Process msedge.exe; Start-Process notepad.exe")
    with pytest.raises(ShellBoundaryError, match="background or detached"):
        analyze_shell_command("Start-Process msedge.exe -ArgumentList $(Start-Process notepad.exe)")
    with pytest.raises(ShellBoundaryError, match="background or detached"):
        analyze_shell_command('powershell -Command "Start-Process msedge.exe"')


def test_prompt_reflects_manual_browser_launch_override():
    disabled = build_runtime_prompt(
        tool_registry=None,
        app_config={"security": {"allow_browser_process_launch": False}},
    )
    enabled = build_runtime_prompt(
        tool_registry=None,
        app_config={"security": {"allow_browser_process_launch": True}},
    )

    assert "不要用 shell 手动启动调试浏览器" in disabled
    assert "演示模式：允许命令启动浏览器" in enabled
    assert "可以用 run_shell_command 启动受支持的浏览器进程" in enabled
    assert "不要用 shell 手动启动调试浏览器" not in enabled


def test_security_normalization_preserves_manual_override():
    assert normalize_security_config({"allow_browser_process_launch": True})[
        "allow_browser_process_launch"
    ] is True


def test_manual_override_changes_browser_match_not_registry_visibility():
    available = {
        "browser_goto", "browser_read", "browser_act", "browser_extract",
        "run_shell_command", "run_script", "launch_desktop_app",
    }

    default_route = route_tools_for_turn("打开哔哩哔哩", available)
    demo_route = route_tools_for_turn(
        "打开哔哩哔哩",
        available,
        allow_browser_process_launch=True,
    )

    browser_tools = {"browser_goto", "browser_read", "browser_act", "browser_extract"}
    assert default_route.tool_names == available
    assert demo_route.tool_names == available
    assert default_route.matched_tool_names == browser_tools
    assert demo_route.matched_tool_names == browser_tools | {"run_shell_command"}
    assert "run_script" not in demo_route.matched_tool_names
    assert "launch_desktop_app" not in demo_route.matched_tool_names


def test_live_runtime_config_refreshes_shell_policy_and_prompt():
    class FakeConfirmGate:
        def set_explanation_provider(self, provider):
            self.provider = provider

        def set_auto_approve(self, enabled):
            self.auto_approve = enabled

    engine = AgentEngine.__new__(AgentEngine)
    engine._confirm_gate = FakeConfirmGate()
    engine._llm = object()
    engine._registry = None
    engine._uses_config_prompt = True

    engine.apply_runtime_config({
        "approval": {"llm_explain_enabled": False, "auto_approve_tools": False},
        "security": {"allow_browser_process_launch": True},
    })

    assert browser_process_launch_allowed() is True
    assert "演示模式：允许命令启动浏览器" in engine._static_prompt

    engine.apply_runtime_config({
        "approval": {"llm_explain_enabled": False, "auto_approve_tools": False},
        "security": {"allow_browser_process_launch": False},
    })

    assert browser_process_launch_allowed() is False
    assert "不要用 shell 手动启动调试浏览器" in engine._static_prompt
