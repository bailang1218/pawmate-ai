"""WebSocket protocol adapter for typed EventBus events."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pawmate.bridge.contracts import (
    AppEvent,
    ErrorEvent,
    FinishedEvent,
    TextDeltaEvent,
    ToolDoneEvent,
    ToolErrorEvent,
    ToolNotifyEvent,
    ToolStartEvent,
)
from pawmate.core.redaction import redact_text


class WsInboundType(str, Enum):
    CHAT = "chat"
    PING = "ping"


class WsOutboundType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_START = "tool_start"
    TOOL_DONE = "tool_done"
    TOOL_ERROR = "tool_error"
    TOOL_NOTIFY = "tool_notify"
    FINISHED = "finished"
    ERROR = "error"
    PONG = "pong"


@dataclass(frozen=True)
class WsChatInbound:
    text: str


@dataclass(frozen=True)
class WsPingInbound:
    pass


@dataclass(frozen=True)
class WsInvalidInbound:
    error_text: str


WsInbound = WsChatInbound | WsPingInbound | WsInvalidInbound


def parse_inbound(raw: str) -> WsInbound:
    """Parse the existing inbound WebSocket JSON protocol."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return WsInvalidInbound("JSON 解析失败")

    msg_type = data.get("type", "")
    if msg_type == WsInboundType.CHAT.value:
        return WsChatInbound(text=data.get("text", "").strip())
    if msg_type == WsInboundType.PING.value:
        return WsPingInbound()
    return WsInvalidInbound(f"未知消息类型: {msg_type}")


def to_ws_payload(event: AppEvent) -> dict[str, Any] | None:
    """Translate typed app events into the existing WebSocket JSON protocol."""
    if isinstance(event, TextDeltaEvent):
        return {"type": WsOutboundType.TEXT_DELTA.value, "text": redact_text(event.text)}
    if isinstance(event, ToolStartEvent):
        return {
            "type": WsOutboundType.TOOL_START.value,
            "tool": event.tool_name,
            "input": redact_text(event.input_json),
        }
    if isinstance(event, ToolDoneEvent):
        return {
            "type": WsOutboundType.TOOL_DONE.value,
            "tool": event.tool_name,
            "result": redact_text(event.result)[:200],
        }
    if isinstance(event, ToolErrorEvent):
        return {
            "type": WsOutboundType.TOOL_ERROR.value,
            "tool": event.tool_name,
            "error": redact_text(event.error_msg),
        }
    if isinstance(event, ToolNotifyEvent):
        return {
            "type": WsOutboundType.TOOL_NOTIFY.value,
            "tool": event.tool_name,
            "input": redact_text(event.input_json),
        }
    if isinstance(event, FinishedEvent):
        return {"type": WsOutboundType.FINISHED.value}
    if isinstance(event, ErrorEvent):
        return {"type": WsOutboundType.ERROR.value, "text": redact_text(event.message)}
    return None


def pong_payload() -> dict[str, str]:
    return {"type": WsOutboundType.PONG.value}


def error_payload(message: str) -> dict[str, str]:
    return {"type": WsOutboundType.ERROR.value, "text": message}
