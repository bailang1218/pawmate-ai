"""Provider-native web search adapters.

This module intentionally does not control a browser. It calls hosted search
features exposed by model providers and returns a normalized result. Browser
automation remains behind the browser facade tools.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx

from pawmate import config as pawmate_config
from pawmate.core.model.llm_factory import (
    DEFAULT_LLM_CONFIG,
    _load_config,
    _normalize_config,
    resolve_llm_config,
)
from pawmate.core.model.model_catalog import normalize_provider_key
from pawmate.core.safety.redaction import redact_text
from pawmate.tools.core.registry import (
    APPROVAL_NOTIFY,
    RiskLevel,
    SideEffectLevel,
    ToolCategory,
    ToolDef,
    ToolRegistry,
)


_logger = logging.getLogger("pawmate.operation")

SUPPORTED_PROVIDERS = ("deepseek", "qwen", "openai", "anthropic", "gemini", "minimaxi")
DEFAULT_PROVIDER_ORDER = ("deepseek", "qwen", "openai", "anthropic", "gemini", "minimaxi")

DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "minimaxi": "https://api.minimax.io/v1",
}

ALT_API_KEY_ENV = {
    "qwen": ("DASHSCOPE_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "minimaxi": ("MINIMAX_API_KEY", "MINIMAXI_API_KEY"),
}

BOUNDARY = {
    "kind": "provider_native_web_search",
    "browser_session": False,
    "uses_login_state": False,
    "can_click_or_extract_dom": False,
}


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    api_key: str
    model: str
    base_url: str
    configured: bool
    source: dict[str, str | None]


@dataclass(frozen=True)
class SearchOptions:
    query: str
    freshness_days: int | None
    max_sources: int
    forced: bool
    allowed_domains: tuple[str, ...]
    blocked_domains: tuple[str, ...]


class NativeWebSearchError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


Adapter = Callable[[ProviderSettings, SearchOptions], Awaitable[dict[str, Any]]]


def _clean_domains(domains: list[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for domain in domains or []:
        item = str(domain or "").strip().lower()
        item = item.removeprefix("http://").removeprefix("https://").strip("/")
        if not item or any(ch.isspace() for ch in item):
            continue
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result[:20])


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(minimum, min(maximum, number))


def _current_provider() -> str:
    config = _normalize_config(_load_config())
    llm = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    return normalize_provider_key(str(llm.get("provider") or "deepseek"))


def _configured_chat_routes() -> list[str]:
    config = _normalize_config(_load_config())
    llm = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    routes = llm.get("routes", {}) if isinstance(llm.get("routes"), dict) else {}
    chat_routes = routes.get("chat", [])
    result: list[str] = []
    if not isinstance(chat_routes, list):
        return result
    for item in chat_routes:
        if isinstance(item, str):
            name = normalize_provider_key(item)
        elif isinstance(item, dict):
            name = normalize_provider_key(str(item.get("provider") or ""))
        else:
            name = ""
        if name and name in SUPPORTED_PROVIDERS and name not in result:
            result.append(name)
    return result


def _provider_order(requested_provider: str, *, allow_fallback: bool = True) -> list[str]:
    requested = normalize_provider_key(str(requested_provider or "auto"))
    current = _current_provider()
    fallback_config = [
        normalize_provider_key(provider)
        for provider in getattr(pawmate_config, "FALLBACK_PROVIDERS", [])
        if str(provider or "").strip()
    ]

    if requested == "current":
        candidates = [current]
    elif requested == "auto":
        candidates = [current, *_configured_chat_routes(), *fallback_config, *DEFAULT_PROVIDER_ORDER]
    elif requested in SUPPORTED_PROVIDERS:
        candidates = [requested]
        if allow_fallback:
            candidates.extend([current, *_configured_chat_routes(), *fallback_config, *DEFAULT_PROVIDER_ORDER])
    else:
        candidates = [current, *_configured_chat_routes(), *fallback_config, *DEFAULT_PROVIDER_ORDER]

    result: list[str] = []
    for name in candidates:
        normalized = normalize_provider_key(name)
        if normalized in SUPPORTED_PROVIDERS and normalized not in result:
            result.append(normalized)
    return result


def _resolve_settings(provider: str) -> ProviderSettings:
    provider = normalize_provider_key(provider)
    config = _normalize_config(_load_config())
    llm = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    provider_config = llm.get(provider, {})
    if not isinstance(provider_config, dict):
        provider_config = {}
    defaults = DEFAULT_LLM_CONFIG.get(provider, {})
    resolved = resolve_llm_config(
        provider=provider,
        provider_config=provider_config,
        defaults=defaults,
        kwargs={},
    )

    api_key = str(resolved.get("api_key") or "").strip()
    source = dict(resolved.get("sources") or {})
    if not api_key:
        for env_name in ALT_API_KEY_ENV.get(provider, ()):
            value = str(os.getenv(env_name) or "").strip()
            if value:
                api_key = value
                source["api_key"] = f"env:{env_name}"
                break

    model = str(resolved.get("model") or "").strip()
    base_url = str(resolved.get("base_url") or "").strip() or DEFAULT_BASE_URLS.get(provider, "")
    configured = bool(api_key and model)
    return ProviderSettings(
        name=provider,
        api_key=api_key,
        model=model,
        base_url=base_url.rstrip("/"),
        configured=configured,
        source=source,
    )


def _headers(api_key: str, *, accept: str = "application/json", extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": accept,
        "Connection": "close",
    }
    if extra:
        headers.update(extra)
    return headers


async def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float = 70.0,
    attempts: int = 2,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=timeout_seconds, write=30.0, pool=5.0),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=0, keepalive_expiry=0.0),
                http2=False,
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
            body = response.text
            if response.status_code < 400:
                try:
                    return response.json()
                except Exception as exc:
                    raise NativeWebSearchError("invalid_json", f"Provider returned non-JSON response: {body[:240]}") from exc

            if response.status_code in {408, 409, 425, 429} or 500 <= response.status_code < 600:
                last_error = NativeWebSearchError(
                    f"http_{response.status_code}",
                    f"HTTP {response.status_code}: {redact_text(body[:500])}",
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

            raise NativeWebSearchError(
                f"http_{response.status_code}",
                f"HTTP {response.status_code}: {redact_text(body[:500])}",
            )
        except NativeWebSearchError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise NativeWebSearchError(type(exc).__name__, redact_text(str(exc))) from exc

    raise NativeWebSearchError(type(last_error).__name__, redact_text(str(last_error))) from last_error


def _extract_choice_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict)).strip()
    return ""


def _extract_openai_response_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _extract_dashscope_text(data: dict[str, Any]) -> str:
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    choices = output.get("choices") if isinstance(output.get("choices"), list) else []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
    return ""


def _extract_anthropic_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _extract_gemini_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return "\n".join(str(part.get("text") or "").strip() for part in parts if isinstance(part, dict)).strip()


def _find_first_url(mapping: dict[str, Any]) -> str:
    for key in ("url", "uri", "link", "href", "source_url", "web_url"):
        value = mapping.get(key)
        if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
            return value.strip()
    return ""


def _first_text(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _collect_sources(payload: Any, *, limit: int) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()

    def walk(value: Any, depth: int) -> None:
        if len(sources) >= limit or depth > 9:
            return
        if isinstance(value, dict):
            web = value.get("web") if isinstance(value.get("web"), dict) else None
            target = web or value
            url = _find_first_url(target)
            if url and url not in seen:
                seen.add(url)
                title = _first_text(target, ("title", "name", "site_name", "source"))
                snippet = _first_text(target, ("snippet", "summary", "text", "content"))
                sources.append(
                    {
                        "title": title[:240],
                        "url": url,
                        "snippet": snippet[:500],
                    }
                )
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(payload, 0)
    return sources


def _normalize_evidence_sources(
    sources: list[dict[str, str]],
    *,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()

    def domain_matches(hostname: str, domain: str) -> bool:
        clean = domain.split("/", 1)[0].split(":", 1)[0].lower().strip(".")
        return bool(clean and (hostname == clean or hostname.endswith(f".{clean}")))

    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        try:
            parsed = urlparse(url)
        except Exception:
            continue
        hostname = str(parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not hostname:
            continue
        if blocked_domains and any(domain_matches(hostname, domain) for domain in blocked_domains):
            continue
        if allowed_domains and not any(domain_matches(hostname, domain) for domain in allowed_domains):
            continue
        canonical_url = parsed.geturl()
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        normalized.append({
            "source_id": f"S{len(normalized) + 1}",
            "title": str(source.get("title") or hostname).strip()[:240],
            "url": canonical_url,
            "snippet": str(source.get("snippet") or "").strip()[:500],
        })
    return normalized


def _with_domain_prompt(options: SearchOptions) -> str:
    query = (
        f"{options.query}\n\n"
        f"Current local date: {date.today().isoformat()}. "
        "Resolve relative dates such as today, tomorrow, this week, 今天, 明天, 今日, and 最近 using this date. "
        "Only state claims supported by the returned web sources. If the sources do not support a claim, say it is unverified."
    )
    constraints: list[str] = []
    if options.allowed_domains:
        constraints.append("Prefer sources from: " + ", ".join(options.allowed_domains))
    if options.blocked_domains:
        constraints.append("Avoid sources from: " + ", ".join(options.blocked_domains))
    if not constraints:
        return query
    return query + "\n\nSearch constraints:\n" + "\n".join(f"- {item}" for item in constraints)


def _freshness_value(days: int | None) -> int | None:
    if days is None:
        return None
    if days <= 0:
        return None
    if days <= 7:
        return 7
    if days <= 30:
        return 30
    if days <= 180:
        return 180
    return 365


def _search_options_payload(options: SearchOptions) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enable_source": True,
        "enable_citation": True,
        "citation_format": "[ref_<number>]",
    }
    freshness = _freshness_value(options.freshness_days)
    if freshness is not None:
        payload["freshness"] = freshness
    if options.allowed_domains:
        payload["assigned_site_list"] = list(options.allowed_domains)
    if options.forced:
        payload["forced_search"] = True
    return payload


def _normalized_result(
    *,
    provider: str,
    model: str,
    query: str,
    answer: str,
    sources: list[dict[str, str]],
    raw: dict[str, Any],
    attempts: list[dict[str, Any]],
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not answer.strip():
        raise NativeWebSearchError("empty_answer", "Provider returned no answer text")
    if _looks_like_unresolved_tool_markup(answer):
        raise NativeWebSearchError(
            "unresolved_tool_markup",
            "Provider returned an unresolved hosted-search tool invocation instead of a final answer.",
        )
    evidence_sources = _normalize_evidence_sources(
        sources,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )
    if not evidence_sources:
        raise NativeWebSearchError(
            "missing_evidence_sources",
            "Provider returned an answer without any valid web source URLs.",
        )
    return {
        "ok": True,
        "operation": "native_web_search",
        "provider": provider,
        "model": model,
        "query": query,
        "answer": answer,
        "grounded": True,
        "evidence_count": len(evidence_sources),
        "citation_required": True,
        "sources": evidence_sources,
        "attempts": attempts,
        "boundary": BOUNDARY,
        "usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
    }


def _looks_like_unresolved_tool_markup(text: str) -> bool:
    lowered = str(text or "").lower()
    return (
        "dsml" in lowered and "tool_calls" in lowered and "web_search" in lowered
    ) or (
        "invoke name=\"web_search\"" in lowered
    ) or (
        "<tool_call" in lowered and "web_search" in lowered
    )


async def _qwen_search(settings: ProviderSettings, options: SearchOptions) -> dict[str, Any]:
    dashscope_payload = {
        "model": settings.model,
        "input": {"messages": [{"role": "user", "content": _with_domain_prompt(options)}]},
        "parameters": {
            "enable_search": True,
            "search_options": _search_options_payload(options),
            "result_format": "message",
        },
    }
    try:
        data = await _post_json(
            _qwen_dashscope_endpoint(settings),
            headers=_headers(settings.api_key),
            payload=dashscope_payload,
        )
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        return {
            "answer": _extract_dashscope_text(data),
            "sources": _collect_sources(output.get("search_info") or data, limit=options.max_sources),
            "raw": data,
        }
    except NativeWebSearchError as dashscope_error:
        _logger.info(
            "[NativeWebSearch] qwen dashscope path failed, trying compatible path: %s",
            dashscope_error.error_type,
        )

    compatible_payload = {
        "model": settings.model,
        "messages": [{"role": "user", "content": _with_domain_prompt(options)}],
        "stream": False,
        "enable_search": True,
        "search_options": _search_options_payload(options),
    }
    data = await _post_json(
        f"{settings.base_url.rstrip('/')}/chat/completions",
        headers=_headers(settings.api_key),
        payload=compatible_payload,
    )
    answer = _extract_choice_text(data)
    return {
        "answer": answer,
        "sources": _collect_sources(data, limit=options.max_sources),
        "raw": data,
    }


def _qwen_dashscope_endpoint(settings: ProviderSettings) -> str:
    base = settings.base_url.rstrip("/")
    if "/compatible-mode/v1" in base:
        return base.split("/compatible-mode/v1", 1)[0].rstrip("/") + "/api/v1/services/aigc/text-generation/generation"
    if base.endswith("/api/v1"):
        return base + "/services/aigc/text-generation/generation"
    if "dashscope.aliyuncs.com" in base:
        return "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    if "maas.aliyuncs.com" in base:
        root = base.split("/api/", 1)[0].split("/compatible-mode", 1)[0].rstrip("/")
        return root + "/api/v1/services/aigc/text-generation/generation"
    return "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"


async def _openai_search(settings: ProviderSettings, options: SearchOptions) -> dict[str, Any]:
    payload = {
        "model": settings.model,
        "input": _with_domain_prompt(options),
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "max_output_tokens": 1400,
    }
    data = await _post_json(
        f"{settings.base_url.rstrip('/')}/responses",
        headers=_headers(settings.api_key),
        payload=payload,
    )
    answer = _extract_openai_response_text(data)
    return {
        "answer": answer,
        "sources": _collect_sources(data, limit=options.max_sources),
        "raw": data,
    }


def _anthropic_endpoint(settings: ProviderSettings) -> str:
    base = settings.base_url.rstrip("/")
    if settings.name == "deepseek":
        if base.endswith("/anthropic"):
            return f"{base}/v1/messages"
        if "/anthropic/" in base:
            return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        return f"{base}/anthropic/v1/messages"
    return f"{base}/v1/messages"


async def _anthropic_family_search(settings: ProviderSettings, options: SearchOptions) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max(1, min(5, options.max_sources)),
    }
    if options.allowed_domains:
        tool["allowed_domains"] = list(options.allowed_domains)
    if options.blocked_domains:
        tool["blocked_domains"] = list(options.blocked_domains)

    payload = {
        "model": settings.model,
        "max_tokens": 1400,
        "messages": [{"role": "user", "content": _with_domain_prompt(options)}],
        "tools": [tool],
    }
    headers = _headers(
        settings.api_key,
        extra={
            "x-api-key": settings.api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    data = await _post_json(
        _anthropic_endpoint(settings),
        headers=headers,
        payload=payload,
    )
    answer = _extract_anthropic_text(data)
    return {
        "answer": answer,
        "sources": _collect_sources(data, limit=options.max_sources),
        "raw": data,
    }


async def _gemini_search(settings: ProviderSettings, options: SearchOptions) -> dict[str, Any]:
    base = settings.base_url
    if "generativelanguage.googleapis.com" not in base:
        base = DEFAULT_BASE_URLS["gemini"]
    if "/openai" in base:
        base = base.split("/openai", 1)[0].rstrip("/")
    model = settings.model.removeprefix("models/")
    endpoint = f"{base.rstrip('/')}/models/{quote(model, safe='')}:generateContent?key={quote(settings.api_key, safe='')}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": _with_domain_prompt(options)}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1400},
    }
    data = await _post_json(
        endpoint,
        headers={"Content-Type": "application/json", "Accept": "application/json", "Connection": "close"},
        payload=payload,
    )
    answer = _extract_gemini_text(data)
    return {
        "answer": answer,
        "sources": _collect_sources(data, limit=options.max_sources),
        "raw": data,
    }


async def _minimaxi_search(settings: ProviderSettings, options: SearchOptions) -> dict[str, Any]:
    raise NativeWebSearchError(
        "provider_unsupported",
        "MiniMax web search is exposed through its Token Plan MCP; no direct native HTTP adapter is configured.",
    )


_ADAPTERS: dict[str, Adapter] = {
    "deepseek": _anthropic_family_search,
    "anthropic": _anthropic_family_search,
    "qwen": _qwen_search,
    "openai": _openai_search,
    "gemini": _gemini_search,
    "minimaxi": _minimaxi_search,
}


def _attempt(provider: str, status: str, *, error_type: str = "", message: str = "", model: str = "") -> dict[str, Any]:
    result = {"provider": provider, "status": status}
    if model:
        result["model"] = model
    if error_type:
        result["error_type"] = error_type
    if message:
        result["message"] = redact_text(message[:500])
    return result


async def native_web_search(
    query: str,
    provider: str = "auto",
    freshness_days: int | None = None,
    max_sources: int = 6,
    forced: bool = True,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    fallback: bool = True,
) -> dict[str, Any]:
    """Run provider-native web search without opening or controlling a browser."""
    clean_query = str(query or "").strip()
    if not clean_query:
        return {
            "ok": False,
            "operation": "native_web_search",
            "error_type": "empty_query",
            "message": "query is required",
            "boundary": BOUNDARY,
        }

    options = SearchOptions(
        query=clean_query,
        freshness_days=freshness_days if freshness_days is None else _clamp_int(freshness_days, 7, 1, 365),
        max_sources=_clamp_int(max_sources, 6, 1, 10),
        forced=bool(forced),
        allowed_domains=_clean_domains(allowed_domains),
        blocked_domains=_clean_domains(blocked_domains),
    )
    attempts: list[dict[str, Any]] = []
    order = _provider_order(provider, allow_fallback=bool(fallback))

    _logger.info("[NativeWebSearch] query chars=%s order=%s", len(clean_query), " -> ".join(order))

    for provider_name in order:
        settings = _resolve_settings(provider_name)
        adapter = _ADAPTERS.get(provider_name)
        if adapter is None:
            attempts.append(_attempt(provider_name, "skipped", error_type="provider_unsupported"))
            continue
        if not settings.configured:
            attempts.append(_attempt(provider_name, "skipped", error_type="not_configured", model=settings.model))
            continue

        try:
            raw_result = await adapter(settings, options)
            normalized = _normalized_result(
                provider=provider_name,
                model=settings.model,
                query=clean_query,
                answer=str(raw_result.get("answer") or ""),
                sources=list(raw_result.get("sources") or [])[: options.max_sources],
                raw=dict(raw_result.get("raw") or {}),
                attempts=[*attempts, _attempt(provider_name, "succeeded", model=settings.model)],
                allowed_domains=options.allowed_domains,
                blocked_domains=options.blocked_domains,
            )
            _logger.info(
                "[NativeWebSearch] provider=%s model=%s sources=%s",
                provider_name,
                settings.model,
                len(normalized.get("sources") or []),
            )
            return normalized
        except NativeWebSearchError as exc:
            attempts.append(
                _attempt(
                    provider_name,
                    "failed",
                    error_type=exc.error_type,
                    message=exc.message,
                    model=settings.model,
                )
            )
            _logger.warning("[NativeWebSearch] provider=%s failed: %s", provider_name, exc.error_type)
        except Exception as exc:
            attempts.append(
                _attempt(
                    provider_name,
                    "failed",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    model=settings.model,
                )
            )
            _logger.warning("[NativeWebSearch] provider=%s failed: %s", provider_name, type(exc).__name__)

    return {
        "ok": False,
        "operation": "native_web_search",
        "error_type": "all_providers_failed",
        "message": "No configured provider-native web search adapter succeeded.",
        "query": clean_query,
        "attempts": attempts,
        "boundary": BOUNDARY,
    }


def register_native_web_search_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDef(
            name="native_web_search",
            description=(
                "Use provider-native hosted web search for public real-time facts, news, weather, stocks, "
                "and general web research. This does not open a browser, use cookies/login state, click pages, "
                "or extract the current DOM. Use browser_goto/read/act/extract only for real browser sessions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "current", "deepseek", "qwen", "openai", "anthropic", "gemini", "minimaxi"],
                        "default": "auto",
                    },
                    "freshness_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 365},
                    "max_sources": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
                    "forced": {"type": "boolean", "default": True},
                    "allowed_domains": {"type": "array", "items": {"type": "string"}, "default": []},
                    "blocked_domains": {"type": "array", "items": {"type": "string"}, "default": []},
                    "fallback": {"type": "boolean", "default": True},
                },
                "required": ["query"],
            },
            handler=native_web_search,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.NETWORK,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
            tags=["web_search", "provider_native", "read_only", "operation_log"],
            timeout=90,
        )
    )
