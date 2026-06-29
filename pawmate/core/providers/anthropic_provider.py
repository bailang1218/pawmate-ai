"""
Anthropic 提供商实现

将现有的 LLMClient 逻辑迁移到此提供商
"""
import json
from typing import AsyncIterator, Dict, Any, Optional, List
from anthropic import AsyncAnthropic

from pawmate.core.llm_provider import LLMProvider, LLMProviderFactory


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API 提供商"""
    
    def __init__(self, api_key: str, model: str, **kwargs):
        super().__init__(api_key, model)
        self._client = AsyncAnthropic(api_key=api_key)
    
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 4096
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式调用 Anthropic API
        
        Args:
            messages: 消息历史（标准格式）
            system: 系统提示词
            tools: 工具定义列表（可选）
            max_tokens: 最大 token 数
            
        Yields:
            标准化事件格式
        """
        # 将消息转换为 Anthropic 格式（如需要）
        anthropic_messages = self.format_messages(messages)
        
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": anthropic_messages,
        }
        
        if tools:
            kwargs["tools"] = self.format_tools(tools)
        
        current_tool_use: Optional[Dict[str, Any]] = None
        current_tool_input_parts = []
        
        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                # 处理不同的事件类型
                if event.type == "content_block_start":
                    if hasattr(event, 'content_block'):
                        cb = event.content_block
                        if hasattr(cb, 'type') and cb.type == "tool_use":
                            current_tool_use = {
                                "type": "tool_use",
                                "id": cb.id,
                                "name": cb.name,
                                "input": {}
                            }
                            current_tool_input_parts = []
                
                elif event.type == "content_block_delta":
                    if hasattr(event, 'delta'):
                        delta = event.delta
                        if hasattr(delta, 'type'):
                            if delta.type == "text_delta":
                                yield {
                                    "type": "text_delta",
                                    "text": delta.text
                                }
                            elif delta.type == "input_json_delta":
                                # 累积 tool_use 的 input 部分
                                if current_tool_use is not None:
                                    current_tool_input_parts.append(delta.partial_json)
                
                elif event.type == "content_block_stop":
                    if current_tool_use is not None:
                        # 解析完整的 JSON input
                        full_input_json = "".join(current_tool_input_parts)
                        try:
                            current_tool_use["input"] = json.loads(full_input_json)
                        except json.JSONDecodeError:
                            # 如果解析失败，保存为字符串
                            current_tool_use["input"] = {"raw": full_input_json}
                        
                        yield current_tool_use
                        current_tool_use = None
                        current_tool_input_parts = []
            
            # 保存最终响应
            self._last_response = await stream.get_final_message()
    
    def format_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Anthropic 工具格式已是标准格式，直接返回
        
        标准格式示例：
        {
            "name": "get_weather",
            "description": "Get weather info",
            "input_schema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
        """
        return tools
    
    def format_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        将标准消息转换为 Anthropic 兼容格式。
        """
        formatted = []
        for msg in messages:
            role = msg.get("role")

            if role == "user":
                formatted.append({
                    "role": "user",
                    "content": msg.get("content", ""),
                })
                continue

            if role == "assistant":
                content = msg.get("content", "")

                if isinstance(content, dict):
                    blocks = []
                    text = content.get("text", "") or content.get("content", "")
                    if text:
                        blocks.append({"type": "text", "text": text})

                    for tc in content.get("tool_calls", []) or []:
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "input": tc.get("input", {}) or {},
                        })

                    formatted.append({
                        "role": "assistant",
                        "content": blocks if blocks else "",
                    })
                else:
                    formatted.append({
                        "role": "assistant",
                        "content": content,
                    })
                continue

            if role == "tool":
                formatted.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_use_id", ""),
                            "content": str(msg.get("content", "")),
                        }
                    ],
                })
                continue

            formatted.append(msg)

        return formatted
    
    def get_last_response(self) -> Optional[Any]:
        """获取最后一次调用的完整响应"""
        return self._last_response


# 注册 Anthropic 提供商
LLMProviderFactory.register("anthropic", AnthropicProvider)
