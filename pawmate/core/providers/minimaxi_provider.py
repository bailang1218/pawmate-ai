"""Minimaxi API 提供商实现"""
import os
import json
from typing import AsyncIterator, Dict, Any, Optional, List

from pawmate.core.llm_provider import LLMProvider, LLMProviderFactory
from pawmate.core.providers.http_client import make_streaming_client


class MinimaxiProvider(LLMProvider):
    """Minimaxi API 提供商"""
    
    BASE_URL = "https://api.minimax.chat/v1"

    @staticmethod
    def _parse_tool_arguments(parts: List[str]) -> Dict[str, Any]:
        raw = "".join(parts)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    
    def __init__(self, api_key: str, model: str, **kwargs):
        super().__init__(api_key, model)
        self.group_id = (
            str(kwargs.get("group_id", "")).strip()
            or os.getenv("MINIMAXI_GROUP_ID", "").strip()
            or os.getenv("MINIMAX_GROUP_ID", "").strip()
        )
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
        max_tokens: int = 4096
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 Minimaxi API"""
        minimaxi_messages = self.format_messages(messages)
        minimaxi_messages.insert(0, {"role": "system", "content": system})
        
        payload = {
            "model": self.model,
            "messages": minimaxi_messages,
            "stream": True,
            "max_tokens": max_tokens,
        }
        
        request_kwargs = {"json": payload}
        endpoint = "chat/completions"

        # MiniMax OpenAI 兼容接口支持 GroupId query 参数。
        if self.group_id:
            request_kwargs["params"] = {"GroupId": self.group_id}

        if tools:
            payload["tools"] = self.format_tools(tools)

        async with self._client.stream("POST", endpoint, **request_kwargs) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Minimaxi API 错误 {response.status_code}: {body[:500]}"
                )

            text_parts: List[str] = []
            pending_tool_calls: Dict[int, Dict[str, Any]] = {}
            
            async for line in response.aiter_lines():
                if not line.strip():
                    continue

                data_str = ""
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                elif line.startswith("{"):
                    data_str = line.strip()
                else:
                    continue

                if data_str == "[DONE]":
                    break
                
                try:
                    chunk_data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # MiniMax 原生字段
                if chunk_data.get("reply"):
                    text = str(chunk_data.get("reply", ""))
                    text_parts.append(text)
                    yield {
                        "type": "text_delta",
                        "text": text
                    }
                    continue

                # OpenAI 兼容字段
                choices = chunk_data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                if delta.get("content"):
                    text = str(delta.get("content", ""))
                    text_parts.append(text)
                    yield {
                        "type": "text_delta",
                        "text": text
                    }

                # OpenAI 兼容工具调用增量
                for tc in delta.get("tool_calls", []) or []:
                    index = int(tc.get("index", 0))
                    state = pending_tool_calls.setdefault(
                        index,
                        {
                            "id": "",
                            "name": "",
                            "arguments": [],
                        },
                    )

                    if tc.get("id"):
                        state["id"] = tc["id"]

                    func = tc.get("function", {})
                    if func.get("name"):
                        state["name"] = func["name"]
                    if func.get("arguments"):
                        state["arguments"].append(func["arguments"])

                finish_reason = choice.get("finish_reason")
                if finish_reason == "tool_calls" and pending_tool_calls:
                    for idx in sorted(pending_tool_calls.keys()):
                        state = pending_tool_calls[idx]
                        yield {
                            "type": "tool_use",
                            "id": state.get("id") or f"tool_{idx}",
                            "name": state.get("name", ""),
                            "input": self._parse_tool_arguments(state.get("arguments", [])),
                        }
                    pending_tool_calls.clear()
                elif finish_reason == "length":
                    yield {"type": "truncated"}

            # 兜底：流结束但未收到 finish_reason=tool_calls
            if pending_tool_calls:
                for idx in sorted(pending_tool_calls.keys()):
                    state = pending_tool_calls[idx]
                    yield {
                        "type": "tool_use",
                        "id": state.get("id") or f"tool_{idx}",
                        "name": state.get("name", ""),
                        "input": self._parse_tool_arguments(state.get("arguments", [])),
                    }

            self._last_response = None
    
    def format_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将标准工具格式转换为 Minimaxi 格式"""
        minimaxi_tools = []
        for tool in tools:
            minimaxi_tool = {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {})
                }
            }
            minimaxi_tools.append(minimaxi_tool)
        return minimaxi_tools
    
    def format_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Minimaxi 消息格式与标准格式兼容"""
        minimaxi_messages = []
        for msg in messages:
            if msg.get("role") in ["user", "assistant"]:
                content = msg.get("content", "")
                if msg.get("role") == "assistant" and isinstance(content, dict):
                    assistant_msg = {
                        "role": "assistant",
                        "content": content.get("text", "") or content.get("content", ""),
                    }

                    tool_calls = []
                    for tc in content.get("tool_calls", []) or []:
                        arguments = tc.get("input", {})
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments, ensure_ascii=False)

                        tool_calls.append({
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": arguments,
                            },
                        })

                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls

                    minimaxi_messages.append(assistant_msg)
                else:
                    if isinstance(content, (dict, list)):
                        content = json.dumps(content, ensure_ascii=False)
                    minimaxi_messages.append({
                        "role": msg["role"],
                        "content": content
                    })
            elif msg.get("role") == "tool":
                tool_call_id = msg.get("tool_use_id", "")
                if tool_call_id:
                    minimaxi_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(msg.get("content", "")),
                    })
                else:
                    minimaxi_messages.append({
                        "role": "user",
                        "content": f"Tool {msg.get('name', 'unknown')} result: {msg.get('content', '')}",
                    })
            else:
                minimaxi_messages.append(msg)
        
        return minimaxi_messages


LLMProviderFactory.register("minimaxi", MinimaxiProvider)
