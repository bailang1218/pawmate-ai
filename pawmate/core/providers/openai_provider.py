"""
OpenAI 提供商实现

支持 ChatGPT 和其他 OpenAI 模型
"""
import json
from typing import AsyncIterator, Dict, Any, Optional, List

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from pawmate.core.llm_provider import LLMProvider, LLMProviderFactory


class OpenAIProvider(LLMProvider):
    """OpenAI API 提供商"""
    
    def __init__(self, api_key: str, model: str, **kwargs):
        if AsyncOpenAI is None:
            raise ImportError(
                "openai 库未安装。请运行: pip install openai"
            )
        super().__init__(api_key, model)
        client_kwargs = {"api_key": api_key}
        if kwargs.get("base_url"):
            client_kwargs["base_url"] = str(kwargs["base_url"]).rstrip("/") + "/"
        self._client = AsyncOpenAI(**client_kwargs)

    async def aclose(self) -> None:
        await self._client.close()
    
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 4096
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式调用 OpenAI API
        
        Args:
            messages: 消息历史（标准格式）
            system: 系统提示词
            tools: 工具定义列表（可选）
            max_tokens: 最大 token 数
            
        Yields:
            标准化事件格式
        """
        # 在消息前插入系统消息
        openai_messages = self.format_messages(messages)
        openai_messages.insert(0, {"role": "system", "content": system})
        
        kwargs = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        
        # 处理工具定义
        if tools:
            kwargs["tools"] = self.format_tools(tools)
            kwargs["tool_choice"] = "auto"
        
        current_tool_use = None
        current_tool_arguments = []
        
        async with self._client.chat.completions.create(**kwargs) as stream:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                
                choice = chunk.choices[0]
                delta = choice.delta
                
                # 处理文本内容
                if delta.content:
                    yield {
                        "type": "text_delta",
                        "text": delta.content
                    }
                
                # 处理工具调用
                if delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        if tool_call.id:
                            # 开始新的工具调用
                            if current_tool_use is not None:
                                # 保存前一个工具调用
                                current_tool_use["input"] = self._parse_tool_arguments(current_tool_arguments)
                                yield current_tool_use
                            
                            current_tool_use = {
                                "type": "tool_use",
                                "id": tool_call.id,
                                "name": tool_call.function.name if tool_call.function else "",
                                "input": {}
                            }
                            current_tool_arguments = []
                        
                        # 累积参数
                        if tool_call.function and tool_call.function.arguments:
                            current_tool_arguments.append(tool_call.function.arguments)
            
            # 保存最后一个工具调用
            if current_tool_use is not None:
                current_tool_use["input"] = self._parse_tool_arguments(current_tool_arguments)
                yield current_tool_use
    
    def _parse_tool_arguments(self, arguments_parts: List[str]) -> Dict[str, Any]:
        """解析工具参数"""
        full_arguments = "".join(arguments_parts)
        try:
            return json.loads(full_arguments) if full_arguments else {}
        except json.JSONDecodeError:
            return {"raw": full_arguments}
    
    def format_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        将标准工具格式转换为 OpenAI 格式
        
        OpenAI 格式：
        {
            "type": "function",
            "function": {
                "name": "...",
                "description": "...",
                "parameters": {...}
            }
        }
        """
        openai_tools = []
        for tool in tools:
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {})
                }
            }
            openai_tools.append(openai_tool)
        return openai_tools
    
    def format_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        OpenAI 消息格式与标准格式兼容
        
        但需要处理 tool_result 类型的消息
        """
        openai_messages = []
        for msg in messages:
            if msg.get("role") == "user":
                openai_messages.append({
                    "role": "user",
                    "content": msg.get("content", "")
                })
            elif msg.get("role") == "assistant":
                content = msg.get("content", "")

                if isinstance(content, dict):
                    openai_msg = {
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
                        openai_msg["tool_calls"] = tool_calls

                    openai_messages.append(openai_msg)
                else:
                    openai_messages.append({
                        "role": "assistant",
                        "content": content,
                    })
            elif msg.get("role") == "tool":
                tool_call_id = msg.get("tool_use_id", "")
                if tool_call_id:
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(msg.get("content", "")),
                    })
                else:
                    openai_messages.append({
                        "role": "user",
                        "content": f"Tool {msg.get('name', 'unknown')} returned: {msg.get('content', '')}"
                    })
            else:
                openai_messages.append(msg)
        
        return openai_messages


# 注册 OpenAI 提供商
LLMProviderFactory.register("openai", OpenAIProvider)
