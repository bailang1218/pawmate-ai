"""
LLM 提供商模块

支持多个 LLM API 提供商：
- Anthropic (Claude)
- OpenAI (GPT)
- DeepSeek
- Qwen (千问)
- Gemini
- Minimaxi
"""

# 导入基类和工厂
from pawmate.core.llm_provider import LLMProvider, LLMProviderFactory

# 自动导入所有提供商（使其自动注册）
from pawmate.core.providers.anthropic_provider import AnthropicProvider
from pawmate.core.providers.openai_provider import OpenAIProvider
from pawmate.core.providers.deepseek_provider import DeepSeekProvider
from pawmate.core.providers.qwen_provider import QwenProvider
from pawmate.core.providers.gemini_provider import GeminiProvider
from pawmate.core.providers.minimaxi_provider import MinimaxiProvider

__all__ = [
    "LLMProvider",
    "LLMProviderFactory",
    "AnthropicProvider",
    "OpenAIProvider",
    "DeepSeekProvider",
    "QwenProvider",
    "GeminiProvider",
    "MinimaxiProvider",
]
