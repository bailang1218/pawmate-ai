"""Normalized provider boundary contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ProviderEventType = Literal["text_delta", "tool_use", "truncated", "usage"]


@dataclass(frozen=True)
class NormalizedMessage:
    role: str
    content: Any
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ProviderRequest:
    messages: list[NormalizedMessage]
    system: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 4096


@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    input: dict[str, Any]
    raw_arguments: str | None = None
    provider: str | None = None
    index: int | None = None

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise ValueError("provider tool_use event requires non-empty tool_call_id")
        if not str(self.name or "").strip():
            raise ValueError("provider tool_use event requires non-empty tool name")
        if not isinstance(self.input, dict):
            raise ValueError("provider tool_use event input must be a dict")


@dataclass(frozen=True)
class ProviderStreamChunk:
    type: ProviderEventType
    text: str = ""
    tool_call: NormalizedToolCall | None = None
    usage: dict[str, Any] | None = None
    reason: str = ""
    provider: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    content: Any = None
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class ProviderError:
    provider: str
    error_type: str
    message: str
    retryable: bool = False
    raw: str | None = None


def text_delta_event(text: Any, *, provider: str | None = None) -> dict[str, Any]:
    return {
        "type": "text_delta",
        "text": "" if text is None else str(text),
        "provider": provider,
    }


def truncated_event(reason: str = "", *, provider: str | None = None) -> dict[str, Any]:
    return {
        "type": "truncated",
        "reason": reason,
        "provider": provider,
    }


def usage_event(
    usage: Any,
    *,
    provider: str | None = None,
    estimated: bool = False,
) -> dict[str, Any]:
    usage_dict = _usage_to_dict(usage)
    prompt_tokens = _first_token_value(
        usage_dict,
        "prompt_tokens",
        "input_tokens",
        "prompt",
        "input",
    )
    completion_tokens = _first_token_value(
        usage_dict,
        "completion_tokens",
        "output_tokens",
        "completion",
        "output",
    )
    total_tokens = _first_token_value(
        usage_dict,
        "total_tokens",
        "total",
    )
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)

    estimated_flag = bool(estimated or usage_dict.get("estimated", False))

    return {
        "type": "usage",
        "usage": {
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
            "estimated": estimated_flag,
        },
        "provider": provider,
    }


def tool_use_event(
    tool_call_id: Any,
    name: Any,
    tool_input: Any,
    *,
    raw_arguments: str | None = None,
    provider: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    tool_call = NormalizedToolCall(
        id=str(tool_call_id or "").strip(),
        name=str(name or "").strip(),
        input=tool_input,
        raw_arguments=raw_arguments,
        provider=provider,
        index=index,
    )
    event = {
        "type": "tool_use",
        "id": tool_call.id,
        "name": tool_call.name,
        "input": tool_call.input,
        "provider": provider,
    }
    if raw_arguments is not None:
        event["raw_arguments"] = raw_arguments
    if index is not None:
        event["index"] = index
    return event


def normalize_provider_stream_event(
    event: dict[str, Any],
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    event_type = event.get("type")
    if event_type == "text_delta":
        return text_delta_event(event.get("text", ""), provider=provider or event.get("provider"))
    if event_type == "truncated":
        return truncated_event(str(event.get("reason", "")), provider=provider or event.get("provider"))
    if event_type == "usage":
        usage_payload = event.get("usage", event)
        return usage_event(
            usage_payload,
            provider=provider or event.get("provider"),
            estimated=bool(
                event.get("estimated", False)
                or (isinstance(usage_payload, dict) and usage_payload.get("estimated", False))
            ),
        )
    if event_type == "tool_use":
        return tool_use_event(
            event.get("id"),
            event.get("name"),
            event.get("input", {}),
            raw_arguments=event.get("raw_arguments"),
            provider=provider or event.get("provider"),
            index=event.get("index"),
        )
    raise ValueError(f"unknown provider stream event type: {event_type!r}")


def stream_chunk_to_event(chunk: ProviderStreamChunk) -> dict[str, Any]:
    if chunk.type == "text_delta":
        return text_delta_event(chunk.text, provider=chunk.provider)
    if chunk.type == "truncated":
        return truncated_event(chunk.reason, provider=chunk.provider)
    if chunk.type == "usage":
        return usage_event(chunk.usage or {}, provider=chunk.provider)
    if chunk.type == "tool_use":
        if chunk.tool_call is None:
            raise ValueError("tool_use stream chunk requires tool_call")
        payload = asdict(chunk.tool_call)
        return tool_use_event(
            payload["id"],
            payload["name"],
            payload["input"],
            raw_arguments=payload.get("raw_arguments"),
            provider=chunk.provider or payload.get("provider"),
            index=payload.get("index"),
        )
    raise ValueError(f"unknown provider stream chunk type: {chunk.type!r}")


def _usage_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            dumped = to_dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    try:
        return dict(value)
    except Exception:
        return {}


def _first_token_value(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _coerce_token_count(usage.get(key))
        if value is not None:
            return value
    return None


def _coerce_token_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None
