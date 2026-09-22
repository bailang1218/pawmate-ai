"""Structured runtime trace events for agent runs."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pawmate.core.safety.redaction import redact_sensitive_data


@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    trace_id: str
    run_id: str
    turn_id: str
    event_type: str
    timestamp: float
    tool_call_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "tool_call_id": self.tool_call_id,
            "data": self.data,
        }


class AgentRunTrace:
    def __init__(self, run_id: str, *, trace_id: str | None = None) -> None:
        self.run_id = str(run_id or "")
        self.trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        self.started_at = time.time()
        self.events: list[TraceEvent] = []

    def add_event(
        self,
        event_type: str,
        *,
        turn_id: str = "",
        tool_call_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            trace_id=self.trace_id,
            run_id=self.run_id,
            turn_id=str(turn_id or ""),
            event_type=str(event_type or ""),
            timestamp=time.time(),
            tool_call_id=str(tool_call_id or ""),
            data=_json_safe(redact_sensitive_data(data or {})),
        )
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "event_count": len(self.events),
            "events": [event.to_dict() for event in self.events],
        }

    def summary(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "event_count": len(self.events),
            "event_types": [event.event_type for event in self.events],
        }

    def export_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    def explain(self, *, tool_call_id: str = "") -> list[dict[str, Any]]:
        if not tool_call_id:
            return [event.to_dict() for event in self.events]
        return [
            event.to_dict()
            for event in self.events
            if event.tool_call_id == tool_call_id
        ]


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)
