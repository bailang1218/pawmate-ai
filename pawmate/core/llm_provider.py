"""LLM 提供商抽象接口"""
from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Any, Optional, List


class LLMProvider(ABC):
    """LLM 提供商基类"""
    
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._last_response: Optional[Any] = None
        # 提供商名（由工厂或子类在创建后显式设置）
        self._provider: Optional[str] = None
    
    @abstractmethod
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 4096
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LLM，产出 {"type": "text_delta"|"tool_use", ...}"""
        pass
    
    @abstractmethod
    def format_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """将标准工具格式转换为提供商格式"""
        pass
    
    @abstractmethod
    def format_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将消息转换为提供商格式"""
        pass

    def get_last_response(self) -> Optional[Any]:
        """获取最后一次完整响应（默认实现）。"""
        return self._last_response

    async def aclose(self) -> None:
        """Close provider resources when a runner replaces this client."""
        return None


class LLMProviderFactory:
    """LLM 提供商工厂"""
    
    _providers = {}
    
    @classmethod
    def register(cls, provider_name: str, provider_class):
        """注册提供商"""
        cls._providers[provider_name.lower()] = provider_class
    
    @classmethod
    def create(cls, provider: str, api_key: str, model: str, **kwargs) -> LLMProvider:
        """创建 LLM 提供商实例，支持提供商特定的参数"""
        provider_lower = provider.lower()
        if provider_lower not in cls._providers:
            raise ValueError(
                f"不支持的 LLM 提供商: {provider}。"
                f"支持的提供商: {list(cls._providers.keys())}"
            )
        
        provider_class = cls._providers[provider_lower]
        instance = provider_class(api_key, model, **kwargs)
        instance._provider = provider_lower
        return instance
    
    @classmethod
    def get_available_providers(cls) -> List[str]:
        """获取所有可用提供商"""
        return list(cls._providers.keys())
