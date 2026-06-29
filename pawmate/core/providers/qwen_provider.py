"""
Qwen 提供商实现

Qwen 提供与 OpenAI 兼容的 API。
"""
import json
from typing import AsyncIterator, Dict, Any, Optional, List

from pawmate.core.llm_provider import LLMProvider, LLMProviderFactory
from pawmate.core.providers.http_client import make_streaming_client


class QwenProvider(LLMProvider):
    """Qwen API 提供商（OpenAI 兼容）"""

    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/"

    def __init__(self, api_key: str, model: str, **kwargs):
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
        qwen_messages = self.format_messages(messages)
        qwen_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": self.model,
            "messages": qwen_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if tools:
            payload["tools"] = self.format_tools(tools)

        async with self._client.stream("POST", "chat/completions", json=payload) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"Qwen API 错误 {response.status_code}: {body[:500]}")

            pending_tool_calls: Dict[int, Dict[str, Any]] = {}

            async for line in response.aiter_lines():
                if not line.strip() or line.startswith("data: [DONE]"):
                    continue

                if not line.startswith("data: "):
                    continue

                try:
                    chunk_data = json.loads(line[6:])
                except Exception:
                    continue

                if not chunk_data.get("choices"):
                    continue

                choice = chunk_data["choices"][0]
                delta = choice.get("delta", {})

                if delta.get("content"):
                    yield {"type": "text_delta", "text": delta["content"]}

                self._accumulate_tool_calls(pending_tool_calls, delta.get("tool_calls") or [])

                finish_reason = choice.get("finish_reason")
                if finish_reason == "tool_calls" and pending_tool_calls:
                    for event in self._flush_tool_calls(pending_tool_calls):
                        yield event
                elif finish_reason == "length":
                    yield {"type": "truncated"}

            if pending_tool_calls:
                for event in self._flush_tool_calls(pending_tool_calls):
                    yield event

    @staticmethod
    def _accumulate_tool_calls(
        pending_tool_calls: Dict[int, Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
    ) -> None:
        for tool_call in tool_calls:
            index = int(tool_call.get("index", len(pending_tool_calls)))
            state = pending_tool_calls.setdefault(index, {"id": "", "name": "", "arguments": []})
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
            yield {
                "type": "tool_use",
                "id": state.get("id") or f"tool_{index}",
                "name": state.get("name", ""),
                "input": self._parse_tool_arguments(state.get("arguments", [])),
            }
        pending_tool_calls.clear()

    def _parse_tool_arguments(self, arguments_parts: List[str]) -> Dict[str, Any]:
        full_arguments = "".join(arguments_parts)
        try:
            return json.loads(full_arguments) if full_arguments else {}
        except json.JSONDecodeError:
            return {"raw": full_arguments}

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
        qwen_messages = []
        for msg in messages:
            if msg.get("role") in ["user", "assistant"]:
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), dict):
                    content_dict = msg.get("content", {})
                    assistant_msg = {
                        "role": "assistant",
                        "content": content_dict.get("text", "") or content_dict.get("content", ""),
                    }

                    tool_calls = []
                    for tc in content_dict.get("tool_calls", []) or []:
                        import json as _json
                        arguments = tc.get("input", {})
                        if not isinstance(arguments, str):
                            arguments = _json.dumps(arguments, ensure_ascii=False)

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

                    qwen_messages.append(assistant_msg)
                else:
                    qwen_messages.append({"role": msg["role"], "content": msg.get("content", "")})
            elif msg.get("role") == "tool":
                tool_call_id = msg.get("tool_use_id", "")
                if tool_call_id:
                    qwen_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": str(msg.get("content", "")),
                        }
                    )
                else:
                    qwen_messages.append(
                        {
                            "role": "user",
                            "content": f"Tool {msg.get('name', 'unknown')} result: {msg.get('content', '')}",
                        }
                    )
            else:
                qwen_messages.append(msg)

        return qwen_messages


LLMProviderFactory.register("qwen", QwenProvider)
