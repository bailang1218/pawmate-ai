import pytest

from pawmate.core.safety.confirm_gate import ConfirmGate
from pawmate.core.safety.security_service import SecurityService
from pawmate.core.tools.tool_policy import can_auto_approve, evaluate_tool_policy
from pawmate.tools.core.registry import (
    APPROVAL_AUTO,
    APPROVAL_CONFIRM,
    RiskLevel,
    SideEffectLevel,
    ToolDef,
    ToolRegistry,
)


def _schema():
    return {"type": "object", "properties": {}}


async def _handler():
    return "executed"


def test_policy_marks_confirm_tools_as_requiring_confirmation():
    tool = ToolDef(
        name="write_file",
        description="write",
        input_schema=_schema(),
        handler=_handler,
        approval=APPROVAL_CONFIRM,
    )

    decision = evaluate_tool_policy(tool, {})

    assert decision.allowed is True
    assert decision.requires_confirm is True
    assert decision.reason == "requires_confirmation"


@pytest.mark.asyncio
async def test_registry_policy_denied_does_not_execute_handler_for_hidden_tool():
    called = False

    async def handler():
        nonlocal called
        called = True
        return "executed"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="internal_only",
        description="internal",
        input_schema=_schema(),
        handler=handler,
        model_visible=False,
    ))

    result = await registry.execute("internal_only", {})

    assert called is False
    assert result.to_legacy_string().startswith("[policy_denied]")
    assert registry._last_policy_decision.allowed is False


@pytest.mark.asyncio
async def test_confirm_tool_without_confirm_callback_does_not_execute_handler():
    called = False

    async def handler():
        nonlocal called
        called = True
        return "executed"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="write_file",
        description="write",
        input_schema=_schema(),
        handler=handler,
        approval=APPROVAL_CONFIRM,
    ))

    result = await registry.execute("write_file", {})

    assert called is False
    assert "[approval_unavailable]" in result.to_legacy_string()
    assert registry._last_policy_decision.requires_confirm is True


def test_confirm_gate_auto_approve_cannot_bypass_high_risk_tool_policy():
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="delete_file",
        description="delete",
        input_schema=_schema(),
        handler=_handler,
        risk=RiskLevel.HIGH,
        side_effect=SideEffectLevel.DESTRUCTIVE,
        approval=APPROVAL_CONFIRM,
    ))
    gate = ConfirmGate(registry, event_bus=object())
    gate.set_auto_approve(True)

    assert can_auto_approve(registry.get_tool("delete_file")) is False
    assert gate._can_auto_approve("delete_file") is False


def test_confirm_gate_auto_approve_uses_runtime_browser_policy():
    from pawmate.tools.browser import facade as browser_facade

    registry = ToolRegistry()
    browser_facade.register_browser_facade_tools(registry)
    gate = ConfirmGate(registry, event_bus=object())
    gate.set_auto_approve(True)

    assert gate._can_auto_approve(
        "browser_act",
        {"action": "scroll", "deltaY": 800},
    ) is True
    assert gate._can_auto_approve(
        "browser_act",
        {"action": "click", "intent": "open the selected video"},
    ) is True
    assert gate._can_auto_approve(
        "browser_act",
        {"action": "click", "intent": "pay now"},
    ) is False


@pytest.mark.asyncio
async def test_confirm_gate_auto_approve_does_not_publish_browser_prompt():
    from pawmate.tools.browser import facade as browser_facade

    class FailingEventBus:
        def publish(self, _event):
            raise AssertionError("auto-approved browser action must not open approval UI")

    registry = ToolRegistry()
    browser_facade.register_browser_facade_tools(registry)
    gate = ConfirmGate(registry, event_bus=FailingEventBus())
    gate.set_auto_approve(True)

    assert await gate.request_confirmation(
        "browser_act",
        {"action": "click", "intent": "open the selected video"},
    ) is True
    assert registry._last_approval.reason == "auto_approved"


def test_high_risk_policy_requires_confirm_at_definition_time():
    with pytest.raises(ValueError, match="must use confirm"):
        ToolDef(
            name="run_shell_command",
            description="shell",
            input_schema=_schema(),
            handler=_handler,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.PRIVILEGED,
            approval=APPROVAL_AUTO,
        )


@pytest.mark.asyncio
async def test_security_service_requires_confirm_gate_for_confirmation():
    service = SecurityService(confirm_gate=None)

    assert await service.require_confirmation("write_file", {}) is False
