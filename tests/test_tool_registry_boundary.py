import asyncio

import pytest

from pawmate.tools.core.registry import (
    APPROVAL_AUTO,
    APPROVAL_CONFIRM,
    RiskLevel,
    SideEffectLevel,
    ToolDef,
    ToolRegistry,
)


def _schema(**properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
    }


async def _ok_handler(**kwargs):
    return {"ok": True, "operation": "ok", "echo": kwargs}


def test_tool_def_rejects_missing_schema():
    with pytest.raises(ValueError, match="input_schema"):
        ToolDef(
            name="bad",
            description="bad",
            input_schema={},
            handler=_ok_handler,
        )


@pytest.mark.asyncio
async def test_execute_validates_arguments_before_handler_runs():
    called = False

    async def handler(path: str):
        nonlocal called
        called = True
        return f"read {path}"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="read_text_file",
        description="read",
        input_schema=_schema(path={"type": "string"}),
        handler=handler,
    ))

    with pytest.raises(ValueError, match="missing required argument"):
        await registry.execute("read_text_file", {})
    assert called is False

    with pytest.raises(ValueError, match="must be string"):
        await registry.execute("read_text_file", {"path": 123})
    assert called is False

    result = await registry.execute("read_text_file", {"path": "README.md"})
    assert result.to_legacy_string() == "read README.md"
    assert called is True


def test_hidden_tools_do_not_enter_model_visible_schema():
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="internal_debug",
        description="debug",
        input_schema={"type": "object", "properties": {}},
        handler=_ok_handler,
        model_visible=False,
    ))

    assert registry.get_tool("internal_debug") is not None
    assert registry.list_tools() == []


def test_high_risk_tool_must_use_confirm_approval():
    with pytest.raises(ValueError, match="must use confirm"):
        ToolDef(
            name="delete_file",
            description="delete",
            input_schema=_schema(path={"type": "string"}),
            handler=_ok_handler,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.DESTRUCTIVE,
            approval=APPROVAL_AUTO,
        )

    tool = ToolDef(
        name="delete_file",
        description="delete",
        input_schema=_schema(path={"type": "string"}),
        handler=_ok_handler,
        risk=RiskLevel.HIGH,
        side_effect=SideEffectLevel.DESTRUCTIVE,
        approval=APPROVAL_CONFIRM,
    )
    assert tool.requires_confirm is True


def test_register_bulk_uses_register_validation():
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="input_schema"):
        registry.register_bulk({
            "bad": ToolDef(
                name="bad",
                description="bad",
                input_schema={"type": "string"},
                handler=_ok_handler,
            )
        })


@pytest.mark.asyncio
async def test_tool_timeout_is_enforced_for_async_handlers():
    async def slow_handler():
        await asyncio.sleep(0.05)
        return "done"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="slow",
        description="slow",
        input_schema={"type": "object", "properties": {}},
        handler=slow_handler,
        timeout=0.001,
    ))

    with pytest.raises(asyncio.TimeoutError):
        await registry.execute("slow", {})
