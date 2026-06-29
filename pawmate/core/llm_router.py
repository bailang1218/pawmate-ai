"""LLM route selection with provider availability filtering.

Routes express preferences; availability decides the actual provider. A
provider without a real API key is treated as nonexistent by this module.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pawmate.core.model_catalog import (
    default_model,
    normalize_llm_config_keys,
    normalize_provider_key,
    provider_defaults,
)


RouteEntry = str | Dict[str, Any]


DEFAULT_LLM_ROUTES: Dict[str, List[RouteEntry]] = {
    "chat": ["deepseek", "qwen", "openai", "anthropic", "gemini", "minimaxi"],
    "code": ["deepseek", "qwen", "anthropic", "openai", "gemini"],
    "reasoning": ["anthropic", "openai", "deepseek", "qwen", "gemini"],
    "vision": [
        {"provider": "qwen", "model": "qwen3-vl-plus"},
        {"provider": "openai", "model": "gpt-4o"},
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "minimaxi", "model": "MiniMax-VL-01"},
    ],
}


_CODE_HINT = re.compile(
    r"(代码|编程|脚本|函数|类|bug|debug|调试|测试|pytest|lint|commit|git|"
    r"文件|目录|仓库|实现|修复|重构|运行命令|terminal|powershell|"
    r"\bcode\b|\bbug\b|\bdebug\b|\btest\b|\bpytest\b|\blint\b|\bgit\b|\bcommit\b)",
    re.IGNORECASE,
)

_VISION_HINT = re.compile(
    r"(图片|图像|截图|截屏|屏幕|照片|看图|识图|视觉|ocr|image|screenshot|vision)",
    re.IGNORECASE,
)

_REASONING_HINT = re.compile(
    r"(推理|逻辑|分析|判断|规划|方案|架构|权衡|证明|复杂|为什么|怎么设计|"
    r"\breason\b|\breasoning\b|\banaly[sz]e\b|\bplan\b|\barchitecture\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RouteChoice:
    kind: str
    provider: str
    model: Optional[str]
    reason: str


def classify_task_kind(user_input: str) -> str:
    text = (user_input or "").strip()
    if not text:
        return "chat"
    if _VISION_HINT.search(text):
        return "vision"
    if _CODE_HINT.search(text):
        return "code"
    if _REASONING_HINT.search(text):
        return "reasoning"
    return "chat"


def get_available_llm_providers(config_data: Dict[str, Any]) -> set[str]:
    llm_cfg = _normalized_llm_config(config_data)
    defaults = provider_defaults()
    available: set[str] = set()

    for provider, provider_cfg in llm_cfg.items():
        if provider in {"provider", "mode", "routes"}:
            continue
        provider_key = normalize_provider_key(provider)
        if not isinstance(provider_cfg, dict):
            continue
        api_key = _first_real_key(
            provider_cfg.get("api_key"),
            os.getenv(f"{provider_key.upper()}_API_KEY"),
            defaults.get(provider_key, {}).get("api_key"),
        )
        if api_key:
            available.add(provider_key)

    return available


def resolve_llm_route(
    kind: str,
    config_data: Dict[str, Any],
    *,
    user_input: str = "",
) -> RouteChoice:
    candidates = resolve_llm_route_candidates(kind, config_data, user_input=user_input)
    if not candidates:
        route_kind = normalize_route_kind(kind or classify_task_kind(user_input))
        raise RuntimeError(f"当前没有配置可用于 {route_kind} 路由的模型服务 API Key。")
    return candidates[0]


def resolve_llm_route_candidates(
    kind: str,
    config_data: Dict[str, Any],
    *,
    user_input: str = "",
) -> list[RouteChoice]:
    llm_cfg = _normalized_llm_config(config_data)
    route_kind = normalize_route_kind(kind or classify_task_kind(user_input))
    available = get_available_llm_providers(config_data)
    provider_cfgs = {key: value for key, value in llm_cfg.items() if isinstance(value, dict)}

    mode = str(llm_cfg.get("mode", "auto")).strip().lower() or "auto"
    if mode not in {"auto", "router", "route"}:
        provider = normalize_provider_key(str(llm_cfg.get("provider", "deepseek")))
        if provider not in available:
            raise RuntimeError(f"当前配置的模型服务 {provider} 没有有效 API Key，无法使用。")
        return [_choice_from_entry(
            provider,
            route_kind,
            provider_cfgs,
            reason=f"manual provider {provider}",
        )]

    routes = _merged_routes(llm_cfg.get("routes"))
    candidates = routes.get(route_kind, DEFAULT_LLM_ROUTES["chat"])

    choices: list[RouteChoice] = []
    for entry in candidates:
        provider = _entry_provider(entry)
        if not provider:
            continue
        if provider not in available:
            continue
        choices.append(
            _choice_from_entry(
                entry,
                route_kind,
                provider_cfgs,
                reason=f"{route_kind} route matched {provider}",
            )
        )
    if choices:
        return choices

    if route_kind == "vision":
        wanted = ", ".join(_entry_provider(entry) for entry in candidates if _entry_provider(entry))
        raise RuntimeError(
            f"当前没有配置可用于 vision 路由的视觉模型 API Key。候选服务商: {wanted or '无'}。"
        )

    fallback_provider = normalize_provider_key(str(llm_cfg.get("provider", "deepseek")))
    if fallback_provider in available:
        return [_choice_from_entry(
            fallback_provider,
            route_kind,
            provider_cfgs,
            reason=f"{route_kind} route fallback to configured provider {fallback_provider}",
        )]

    if available:
        provider = sorted(available)[0]
        return [_choice_from_entry(
            provider,
            route_kind,
            provider_cfgs,
            reason=f"{route_kind} route fallback to available provider {provider}",
        )]

    wanted = ", ".join(_entry_provider(entry) for entry in candidates if _entry_provider(entry))
    raise RuntimeError(
        f"当前没有配置可用于 {route_kind} 路由的模型服务 API Key。候选服务商: {wanted or '无'}。"
    )


def normalize_route_kind(kind: str) -> str:
    route_kind = str(kind or "").strip().lower()
    if route_kind in DEFAULT_LLM_ROUTES:
        return route_kind
    return "chat"


def _normalized_llm_config(config_data: Dict[str, Any]) -> Dict[str, Any]:
    llm_cfg = config_data.get("llm", {}) if isinstance(config_data, dict) else {}
    normalized = normalize_llm_config_keys(llm_cfg if isinstance(llm_cfg, dict) else {})
    defaults = provider_defaults()
    for provider, default_cfg in defaults.items():
        current = normalized.get(provider)
        merged = dict(default_cfg)
        if isinstance(current, dict):
            merged.update(current)
        normalized[provider] = merged
    normalized.setdefault("provider", "deepseek")
    normalized.setdefault("mode", "auto")
    normalized.setdefault("routes", DEFAULT_LLM_ROUTES)
    return normalized


def _merged_routes(routes_config: Any) -> Dict[str, List[RouteEntry]]:
    routes = {key: list(value) for key, value in DEFAULT_LLM_ROUTES.items()}
    if not isinstance(routes_config, dict):
        return routes
    for key, value in routes_config.items():
        route_kind = normalize_route_kind(str(key))
        if isinstance(value, list):
            routes[route_kind] = value
    return routes


def _entry_provider(entry: RouteEntry) -> str:
    if isinstance(entry, str):
        return normalize_provider_key(entry)
    if isinstance(entry, dict):
        return normalize_provider_key(str(entry.get("provider", "")))
    return ""


def _choice_from_entry(
    entry: RouteEntry,
    kind: str,
    provider_cfgs: Dict[str, Dict[str, Any]],
    *,
    reason: str,
) -> RouteChoice:
    provider = _entry_provider(entry)
    entry_model = ""
    if isinstance(entry, dict):
        entry_model = str(entry.get("model", "")).strip()
    provider_cfg = provider_cfgs.get(provider, {})
    model = entry_model or str(provider_cfg.get("model", "")).strip() or default_model(provider) or None
    return RouteChoice(kind=kind, provider=provider, model=model, reason=reason)


def _first_real_key(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and not _is_placeholder_key(text):
            return text
    return ""


def _is_placeholder_key(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        not lowered
        or "placeholder" in lowered
        or "edit-me" in lowered
        or lowered in {"sk-xxx", "sk-test", "your-api-key", "api-key"}
        or lowered.startswith("your-")
    )
