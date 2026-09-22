"""DeepSeek provider using the OpenAI-compatible chat completions API."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional

from pawmate.core.model.llm_provider import LLMProvider, LLMProviderFactory
from pawmate.core.model.provider_contract import tool_use_event, usage_event
from pawmate.core.model.providers.http_client import make_streaming_client
from pawmate.core.model.providers.message_roles import require_tool_call_id


_logger = logging.getLogger("pawmate")


class DeepSeekProvider(LLMProvider):
    """DeepSeek OpenAI-compatible streaming provider."""

    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        super().__init__(api_key, model)
        self.base_url = str(kwargs.get("base_url") or self.BASE_URL).strip() or self.BASE_URL
        self._client = make_streaming_client(
            base_url=self.base_url,
            api_key=api_key,
            read_timeout=float(kwargs.get("read_timeout", 180.0)),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[Dict[str, Any]]:
        self._last_response = None
        deepseek_messages = self.format_messages(messages)
        deepseek_messages.insert(0, {"role": "system", "content": system})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": deepseek_messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = self.format_tools(tools)

        _logger.info(
            "[DeepSeek] stream request base_url=%s model=%s messages=%s msg_chars=%s tools=%s",
            self.base_url,
            self.model,
            len(deepseek_messages),
            sum(len(str(msg.get("content", ""))) for msg in deepseek_messages),
            len(payload.get("tools", []) or []),
        )

        async with self._client.stream("POST", "chat/completions", json=payload) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"DeepSeek API error {response.status_code}: {body[:500]}")

            pending_tool_calls: Dict[int, Dict[str, Any]] = {}
            content_parts: List[str] = []
            reasoning_parts: List[str] = []
            async for line in response.aiter_lines():
                data_str = self._sse_data(line)
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    break

                try:
                    chunk_data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                usage = chunk_data.get("usage")
                if usage:
                    yield usage_event(usage, provider="deepseek")

                choices = chunk_data.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta") or {}

                reasoning_content = delta.get("reasoning_content")
                if reasoning_content:
                    reasoning_parts.append(str(reasoning_content))

                content = delta.get("content")
                if content:
                    content_parts.append(str(content))
                    yield {"type": "text_delta", "text": content}

                self._accumulate_tool_calls(pending_tool_calls, delta.get("tool_calls") or [])

                if choice.get("finish_reason") == "tool_calls" and pending_tool_calls:
                    for event in self._flush_tool_calls(pending_tool_calls):
                        yield event
                elif choice.get("finish_reason") == "length":
                    yield {"type": "truncated"}

            if pending_tool_calls:
                for event in self._flush_tool_calls(pending_tool_calls):
                    yield event

            self._last_response = SimpleNamespace(
                content=self._build_response_content(content_parts, reasoning_parts)
            )

    @staticmethod
    def _sse_data(line: str) -> str:
        text = (line or "").strip()
        if not text:
            return ""
        if text.startswith("data:"):
            return text[5:].strip()
        if text.startswith("{") or text == "[DONE]":
            return text
        return ""

    @staticmethod
    def _accumulate_tool_calls(
        pending_tool_calls: Dict[int, Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
    ) -> None:
        for tool_call in tool_calls:
            index = int(tool_call.get("index", len(pending_tool_calls)))
            state = pending_tool_calls.setdefault(
                index,
                {"id": "", "name": "", "arguments": []},
            )
            if tool_call.get("id"):
                state["id"] = tool_call["id"]

            function = tool_call.get("function") or {}
            if function.get("name"):
                state["name"] = function["name"]
            if function.get("arguments"):
                state["arguments"].append(function["arguments"])

    def _flush_tool_calls(self, pending_tool_calls: Dict[int, Dict[str, Any]]):
        for index in sorted(pending_tool_calls.keys()):
            state = pending_tool_calls[index]
            raw_arguments = "".join(state.get("arguments", []))
            yield tool_use_event(
                state.get("id"),
                state.get("name", ""),
                self._parse_tool_arguments(state.get("arguments", [])),
                raw_arguments=raw_arguments,
                provider="deepseek",
                index=index,
            )
        pending_tool_calls.clear()

    def _parse_tool_arguments(self, arguments_parts: List[str]) -> Dict[str, Any]:
        raw = "".join(arguments_parts)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def format_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
            for tool in tools
        ]

    def format_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deepseek_messages: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role in ("user", "assistant"):
                content = msg.get("content", "")
                if role == "assistant" and isinstance(content, dict):
                    assistant_msg: Dict[str, Any] = {
                        "role": "assistant",
                        "content": content.get("text", "") or content.get("content", ""),
                    }
                    reasoning_content = content.get("reasoning_content") or msg.get("reasoning_content")
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = str(reasoning_content)
                    tool_calls = []
                    for tc in content.get("tool_calls", []) or []:
                        arguments = tc.get("input", {})
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments, ensure_ascii=False)
                        tool_calls.append(
                            {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": tc.get("name", ""),
                                    "arguments": arguments,
                                },
                            }
                        )
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                    deepseek_messages.append(assistant_msg)
                else:
                    if isinstance(content, (dict, list)):
                        content = json.dumps(content, ensure_ascii=False)
                    entry = {"role": role, "content": content}
                    if role == "assistant" and msg.get("reasoning_content"):
                        entry["reasoning_content"] = str(msg.get("reasoning_content"))
                    deepseek_messages.append(entry)
            elif role == "tool":
                tool_call_id = require_tool_call_id(msg)
                deepseek_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(msg.get("content", "")),
                    }
                )
            else:
                deepseek_messages.append(msg)

        return deepseek_messages

    @staticmethod
    def _build_response_content(content_parts: List[str], reasoning_parts: List[str]) -> Any:
        text = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts)
        if reasoning_content:
            return {
                "text": text,
                "reasoning_content": reasoning_content,
            }
        return text


LLMProviderFactory.register("deepseek", DeepSeekProvider)
