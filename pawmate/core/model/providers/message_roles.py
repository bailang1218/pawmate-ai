"""Provider-side message role guards."""
from __future__ import annotations

from typing import Any


def require_tool_call_id(msg: dict[str, Any]) -> str:
    """Return a non-empty tool call id or reject the malformed tool message."""
    tool_call_id = str(msg.get("tool_use_id") or msg.get("tool_call_id") or "").strip()
    if not tool_call_id:
        raise ValueError("tool message requires non-empty tool_call_id")
    return tool_call_id
