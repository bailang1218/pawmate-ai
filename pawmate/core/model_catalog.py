"""Shared provider and model metadata for PawMate configuration UIs.

The model lists here are convenience suggestions, not a hard allowlist.
Users can still type any provider-supported model name in the UI.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple


PROVIDER_ALIASES: Dict[str, str] = {
    "minimax": "minimaxi",
}


PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "default_model": "claude-opus-4-8",
        "models": [
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ],
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "default_model": "gpt-5.5",
        "models": [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.1",
            "gpt-5",
            "gpt-5-pro",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4o",
            "gpt-4o-mini",
        ],
    },
    "deepseek": {
        "label": "DeepSeek",
        "default_model": "deepseek-v4-flash",
        "models": [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
    },
    "qwen": {
        "label": "Qwen (DashScope)",
        "default_model": "qwen3.7-plus",
        "models": [
            "qwen3.7-plus",
            "qwen3.7-max",
            "qwen3.6-plus",
            "qwen-plus",
            "qwen-plus-latest",
            "qwen3.5-plus",
            "qwen-max",
            "qwen3-max",
            "qwen-turbo",
            "qwen-flash",
            "qwen3.5-flash",
            "qwen3-coder-plus",
            "qwen3-vl-plus",
            "qwq-plus",
        ],
    },
    "gemini": {
        "label": "Google Gemini",
        "default_model": "gemini-2.5-flash",
        "models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-1.5-pro",
        ],
    },
    "minimaxi": {
        "label": "MiniMax",
        "default_model": "MiniMax-M3",
        "models": [
            "MiniMax-M3",
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-Text-01",
            "MiniMax-VL-01",
            "MiniMax-M2.5",
            "MiniMax-M2.5-highspeed",
            "MiniMax-M2.1",
            "MiniMax-M2.1-highspeed",
            "MiniMax-M2",
        ],
        "extra_defaults": {
            "group_id": "",
        },
    },
}


def normalize_provider_key(provider: str) -> str:
    key = str(provider or "").strip().lower()
    return PROVIDER_ALIASES.get(key, key)


def provider_items() -> List[Tuple[str, str]]:
    return [(str(meta["label"]), provider) for provider, meta in PROVIDER_CATALOG.items()]


def provider_labels() -> Dict[str, str]:
    return {provider: str(meta["label"]) for provider, meta in PROVIDER_CATALOG.items()}


def provider_model_options(provider: str) -> List[str]:
    meta = PROVIDER_CATALOG.get(normalize_provider_key(provider), {})
    return list(meta.get("models", []))


def default_model(provider: str) -> str:
    meta = PROVIDER_CATALOG.get(normalize_provider_key(provider), {})
    return str(meta.get("default_model", ""))


def provider_defaults() -> Dict[str, Dict[str, str]]:
    defaults: Dict[str, Dict[str, str]] = {}
    for provider, meta in PROVIDER_CATALOG.items():
        section = {
            "api_key": "",
            "model": str(meta.get("default_model", "")),
        }
        section.update(deepcopy(meta.get("extra_defaults", {})))
        defaults[provider] = section
    return defaults


def build_default_llm_config(default_provider: str = "deepseek") -> Dict[str, Any]:
    return {
        "provider": normalize_provider_key(default_provider) or "deepseek",
        "mode": "auto",
        "routes": {
            "chat": ["deepseek", "qwen", "openai", "anthropic", "gemini", "minimaxi"],
            "code": ["deepseek", "qwen", "anthropic", "openai", "gemini"],
            "reasoning": ["anthropic", "openai", "deepseek", "qwen", "gemini"],
            "vision": [
                {"provider": "qwen", "model": "qwen3-vl-plus"},
                {"provider": "openai", "model": "gpt-4o"},
                {"provider": "gemini", "model": "gemini-2.5-flash"},
                {"provider": "minimaxi", "model": "MiniMax-VL-01"},
            ],
        },
        **provider_defaults(),
    }


def normalize_llm_config_keys(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalize provider names and migrate old alias sections.

    If both an alias section and a canonical section exist, non-empty alias
    values win. This preserves keys saved by older Web settings builds where
    MiniMax was accidentally stored under ``minimax`` instead of ``minimaxi``.
    """
    result = dict(llm_config) if isinstance(llm_config, dict) else {}
    result["provider"] = normalize_provider_key(str(result.get("provider", "deepseek")))

    for alias, canonical in PROVIDER_ALIASES.items():
        alias_cfg = result.pop(alias, None)
        if not isinstance(alias_cfg, dict):
            continue

        canonical_cfg = result.get(canonical)
        if not isinstance(canonical_cfg, dict):
            result[canonical] = dict(alias_cfg)
            continue

        merged = dict(canonical_cfg)
        for key, value in alias_cfg.items():
            if value not in ("", None, [], {}):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        result[canonical] = merged

    return result
