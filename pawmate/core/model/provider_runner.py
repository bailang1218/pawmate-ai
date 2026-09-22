from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Dict, List, Optional

import httpx

import pawmate.config as config
from pawmate.bridge.contracts import TextDeltaEvent
from pawmate.bridge.event_bus import event_bus
from pawmate.core.model.llm_factory import DEFAULT_LLM_CONFIG, get_llm_client
from pawmate.core.model.llm_provider import LLMProvider
from pawmate.core.model.llm_router import RouteChoice
from pawmate.core.model.provider_contract import normalize_provider_stream_event
from pawmate.core.tools.tool_parser import (
    _trailing_partial_marker_start,
    is_truncated_tool_protocol,
    parse_textual_tool_call,
    strip_any_textual_tool_call_protocol,
    visible_text_before_any_textual_tool_protocol,
    visible_text_before_textual_tool_protocol,
)


_logger = logging.getLogger("pawmate")


class ProviderRunner:
    _SAME_PROVIDER_RETRIES = 2

    def __init__(
        self,
        llm_client: LLMProvider,
        *,
        current_provider: Optional[str] = None,
        fallback_providers: Optional[List[str]] = None,
        client_factory: Optional[Callable[..., LLMProvider]] = None,
        status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._llm = llm_client
        self._current_provider = current_provider or self._infer_provider(llm_client)
        self._current_model = getattr(llm_client, "_model", None) or getattr(llm_client, "model", None)
        self._fallback_providers = self._build_fallback_chain(fallback_providers)
        self._fallback_models: Dict[str, Optional[str]] = {}
        self._client_factory = client_factory
        self._status_callback = status_callback
        self._last_attempt_trace: List[Dict[str, Any]] = []
        self._last_usage: Dict[str, Any] = {}

    @property
    def current_provider(self) -> Optional[str]:
        return self._current_provider

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @property
    def llm_client(self) -> LLMProvider:
        return self._llm

    @property
    def fallback_providers(self) -> List[str]:
        return list(self._fallback_providers)

    @fallback_providers.setter
    def fallback_providers(self, providers: List[str]) -> None:
        self._fallback_providers = self._build_fallback_chain(providers)
        self._fallback_models = {}

    def get_last_response(self) -> Any:
        return self._llm.get_last_response()

    def get_last_attempt_trace(self) -> List[Dict[str, Any]]:
        return list(self._last_attempt_trace)

    def get_last_usage(self) -> Dict[str, Any]:
        return dict(self._last_usage)

    def set_status_callback(self, callback: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._status_callback = callback

    def activate_route(self, route: RouteChoice, fallbacks: Optional[List[RouteChoice]] = None) -> None:
        choices = fallbacks or [route]
        self._fallback_providers = []
        self._fallback_models = {}
        for choice in choices:
            if choice.provider in self._fallback_providers:
                continue
            self._fallback_providers.append(choice.provider)
            self._fallback_models[choice.provider] = choice.model

        if (
            self._current_provider == route.provider
            and (route.model is None or self._current_model == route.model)
        ):
            return

        self._llm = self._new_client(route.provider, route.model)
        self._current_provider = route.provider
        self._current_model = getattr(self._llm, "_model", None) or getattr(self._llm, "model", route.model)

    async def collect_stream_events(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools_defs: List[Dict[str, Any]],
        protocol_filter_tools_defs: Optional[List[Dict[str, Any]]] = None,
        final_response: str,
        system_prompt_factory: Callable[[], str],
        turn_id: int = 0,
        max_tokens: int = 8192,
    ) -> tuple[str, str, List[Dict[str, Any]]]:
        current_text = ""
        provider_tried = []
        stream_events = []
        stream_error = None
        visible_text = ""
        filter_tools_defs = protocol_filter_tools_defs if protocol_filter_tools_defs is not None else tools_defs
        self._last_attempt_trace = []
        self._last_usage = {}

        attempt_chain = self._provider_attempt_chain()
        for attempt_idx, provider in enumerate(attempt_chain):
            if attempt_idx > 0:
                previous_providers = list(provider_tried)
                previous_error = self._last_provider_error()
                _logger.warning(
                    "[ProviderRunner] fallback switching provider=%s after=%s",
                    provider,
                    " -> ".join(provider_tried),
                )
                switch_trace = {
                    "event": "provider_fallback_switch",
                    "provider": provider,
                    "after": previous_providers,
                    "message_count": len(messages or []),
                    "tool_count": len(tools_defs or []),
                    "context_inherited": True,
                }
                if previous_error:
                    switch_trace["reason_type"] = previous_error.get("error_type", "")
                    switch_trace["reason"] = self._short_error(previous_error.get("error", ""))
                self._last_attempt_trace.append(switch_trace)
                try:
                    await self._replace_current_client(provider, self._fallback_models.get(provider))
                except Exception as exc:
                    _logger.warning(
                        "[ProviderRunner] fallback client create failed provider=%s error_type=%s error=%s",
                        provider,
                        type(exc).__name__,
                        exc,
                    )
                    self._last_attempt_trace.append({
                        "event": "provider_client_create_error",
                        "provider": provider,
                        "error": str(exc),
                    })
                    self._emit_status({
                        "event": "provider_fallback_failed",
                        "provider": provider,
                        "after": previous_providers,
                        "message_count": len(messages or []),
                        "tool_count": len(tools_defs or []),
                        "error_type": type(exc).__name__,
                        "error": self._short_error(str(exc)),
                        "context_inherited": True,
                    })
                    provider_tried.append(f"{provider}(鍒涘缓澶辫触)")
                    continue
                self._emit_status({
                    **switch_trace,
                    "model": self._current_model or self._fallback_models.get(provider) or "",
                })

            provider_tried.append(provider)
            for stream_attempt in range(self._SAME_PROVIDER_RETRIES):
                self._last_attempt_trace.append({
                    "event": "provider_attempt_started",
                    "provider": provider,
                    "attempt_index": attempt_idx,
                    "stream_attempt": stream_attempt,
                    "message_count": len(messages),
                    "tool_count": len(tools_defs or []),
                })
                try:
                    stream_events = []
                    attempt_system = self._system_for_attempt(
                        system_prompt_factory(),
                        current_text,
                        filter_tools_defs,
                    )
                    async for raw_event in self._llm.stream(
                        messages=self._messages_for_attempt(messages, current_text, filter_tools_defs),
                        system=attempt_system,
                        tools=tools_defs if tools_defs else None,
                        max_tokens=max_tokens,
                    ):
                        event = normalize_provider_stream_event(raw_event, provider=self._current_provider)
                        if event["type"] == "text_delta":
                            text = event["text"]
                            current_text += text
                            final_response += text
                            next_visible_text = self._visible_stream_text(current_text, filter_tools_defs)
                            if next_visible_text.startswith(visible_text):
                                visible_delta = next_visible_text[len(visible_text) :]
                                visible_text = next_visible_text
                                if visible_delta:
                                    event_bus.publish(TextDeltaEvent(visible_delta, turn_id=turn_id))
                        elif event["type"] == "usage":
                            self._last_usage = dict(event.get("usage") or {})
                        else:
                            stream_events.append(event)
                            if event.get("type") == "truncated":
                                stream_error = None
                    stream_error = None
                    final_visible_text = self._final_visible_stream_text(
                        current_text,
                        visible_text,
                        filter_tools_defs,
                    )
                    if final_visible_text.startswith(visible_text):
                        visible_delta = final_visible_text[len(visible_text) :]
                        if visible_delta:
                            event_bus.publish(TextDeltaEvent(visible_delta, turn_id=turn_id))
                        visible_text = final_visible_text
                    self._last_attempt_trace.append({
                        "event": "provider_attempt_succeeded",
                        "provider": provider,
                        "attempt_index": attempt_idx,
                        "stream_attempt": stream_attempt,
                        "text_chars": len(current_text),
                        "stream_event_count": len(stream_events),
                    })
                    break
                except (
                    RuntimeError,
                    ConnectionError,
                    httpx.HTTPStatusError,
                    httpx.ConnectError,
                    httpx.TimeoutException,
                    httpx.RemoteProtocolError,
                ) as exc:
                    retry_same_provider = self._should_retry_same_provider(exc, stream_attempt)
                    _logger.warning(
                        "[ProviderRunner] provider stream failed provider=%s attempt_index=%s "
                        "stream_attempt=%s error_type=%s text_chars=%s message_count=%s "
                        "tool_count=%s retry_same_provider=%s fallback_next=%s error=%s",
                        provider,
                        attempt_idx,
                        stream_attempt,
                        type(exc).__name__,
                        len(current_text or ""),
                        len(messages or []),
                        len(tools_defs or []),
                        retry_same_provider,
                        not retry_same_provider,
                        exc,
                    )
                    stream_error = exc
                    self._last_attempt_trace.append({
                        "event": "provider_attempt_error",
                        "provider": provider,
                        "attempt_index": attempt_idx,
                        "stream_attempt": stream_attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                    if not retry_same_provider:
                        break
                    await self._recreate_current_client(provider)
                    await asyncio.sleep(0.4 * (stream_attempt + 1))
                    continue
                except Exception as exc:
                    if attempt_idx > 0:
                        _logger.warning(
                            "[ProviderRunner] provider non-retryable error provider=%s "
                            "attempt_index=%s stream_attempt=%s error_type=%s error=%s",
                            provider,
                            attempt_idx,
                            stream_attempt,
                            type(exc).__name__,
                            exc,
                        )
                    self._last_attempt_trace.append({
                        "event": "provider_non_retryable_error",
                        "provider": provider,
                        "attempt_index": attempt_idx,
                        "stream_attempt": stream_attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                    raise
            if stream_error is None:
                break

        if stream_error is not None:
            raise RuntimeError(
                f"LLM 璋冪敤鍏ㄩ儴澶辫触锛堝皾璇? {' 鈫?'.join(provider_tried)}锛? {stream_error}"
            )

        return current_text, final_response, stream_events

    def _should_retry_same_provider(self, exc: BaseException, stream_attempt: int) -> bool:
        if stream_attempt + 1 >= self._SAME_PROVIDER_RETRIES:
            return False
        return isinstance(exc, (
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.TimeoutException,
        ))

    async def _recreate_current_client(self, provider: str) -> None:
        model = self._fallback_models.get(provider) or self._current_model
        await self._replace_current_client(provider, model)

    async def _replace_current_client(self, provider: str, model: Optional[str] = None) -> None:
        old_client = self._llm
        new_client = self._new_client(provider, model)
        self._llm = new_client
        self._current_provider = provider
        self._current_model = getattr(new_client, "_model", None) or getattr(new_client, "model", model)

        if old_client is new_client:
            return
        close = getattr(old_client, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass

    @staticmethod
    def _messages_for_attempt(
        messages: List[Dict[str, Any]],
        partial_text: str,
        tools_defs: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        partial = ProviderRunner._strip_partial_tool_protocol(partial_text, tools_defs or []).strip()
        if not partial:
            return messages

        return [
            *messages,
            {"role": "assistant", "content": partial},
        ]

    @staticmethod
    def _system_for_attempt(
        system_prompt: str,
        partial_text: str,
        tools_defs: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        partial = ProviderRunner._strip_partial_tool_protocol(partial_text, tools_defs or []).strip()
        if not partial:
            return system_prompt
        continuation = (
            "The previous model provider failed after the assistant text already present in history. "
            "Continue exactly from where that assistant text stopped. "
            "Do not restart, summarize, or repeat earlier content."
        )
        return f"{system_prompt}\n\n[Runtime continuation]\n{continuation}"

    @staticmethod
    def _visible_stream_text(text: str, tools_defs: List[Dict[str, Any]]) -> str:
        if not tools_defs:
            return visible_text_before_any_textual_tool_protocol(text)
        return visible_text_before_textual_tool_protocol(text, _ToolDefinitionRegistry(tools_defs))

    @staticmethod
    def _strip_partial_tool_protocol(text: str, tools_defs: List[Dict[str, Any]]) -> str:
        if not text:
            return ""
        registry = _ToolDefinitionRegistry(tools_defs)
        stripped = text
        if is_truncated_tool_protocol(stripped, registry):
            stripped = strip_any_textual_tool_call_protocol(stripped)
        marker_start = _trailing_partial_marker_start(stripped)
        if marker_start is not None:
            stripped = stripped[:marker_start]
        return stripped

    @staticmethod
    def _final_visible_stream_text(text: str, visible_text: str, tools_defs: List[Dict[str, Any]]) -> str:
        if not text:
            return visible_text
        registry = _ToolDefinitionRegistry(tools_defs)
        if parse_textual_tool_call(text, registry) is not None:
            return visible_text
        if is_truncated_tool_protocol(text, registry):
            return visible_text
        marker_start = _trailing_partial_marker_start(text)
        if marker_start is None:
            return visible_text
        prefix = text[:marker_start]
        if visible_text == prefix:
            return text
        return visible_text

    def _new_client(self, provider: str, model: Optional[str] = None) -> LLMProvider:
        factory = self._client_factory or get_llm_client
        if model:
            try:
                return factory(provider=provider, model=model)
            except TypeError:
                return factory(provider=provider)
        return factory(provider=provider)

    def _build_fallback_chain(self, fallback_providers: Optional[List[str]]) -> List[str]:
        providers = list(fallback_providers) if fallback_providers is not None else list(
            getattr(config, "FALLBACK_PROVIDERS", [])
        )
        seen: List[str] = []
        if self._current_provider:
            seen.append(self._current_provider)
        for provider in providers:
            if provider not in seen:
                seen.append(provider)
        if not seen:
            seen.append(self._current_provider or "deepseek")
        return seen

    def _provider_attempt_chain(self) -> List[str]:
        seen: List[str] = []
        if self._current_provider:
            seen.append(self._current_provider)
        for provider in self._fallback_providers:
            if provider not in seen:
                seen.append(provider)
        if not seen:
            seen.append("deepseek")
        return seen

    @staticmethod
    def _infer_provider(llm_client: LLMProvider) -> Optional[str]:
        inferred_provider: Optional[str] = getattr(llm_client, "_provider", None)
        if inferred_provider is not None:
            return inferred_provider

        cls_name = type(llm_client).__name__.lower()
        for provider in DEFAULT_LLM_CONFIG:
            if provider == "provider":
                continue
            if provider in cls_name:
                return provider
        return None

    def _emit_status(self, event: Dict[str, Any]) -> None:
        callback = self._status_callback
        if callback is None:
            return
        try:
            callback(dict(event))
        except Exception:
            _logger.debug("[ProviderRunner] status callback failed", exc_info=True)

    def _last_provider_error(self) -> Dict[str, Any]:
        for item in reversed(self._last_attempt_trace):
            if item.get("event") in {"provider_attempt_error", "provider_non_retryable_error"}:
                return dict(item)
        return {}

    @staticmethod
    def _short_error(error: Any, limit: int = 220) -> str:
        text = str(error or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"


class _ToolDefinitionRegistry:
    def __init__(self, tools_defs: List[Dict[str, Any]]) -> None:
        self._names = {str(tool.get("name") or "") for tool in tools_defs}

    def has_tool(self, name: str) -> bool:
        return name in self._names
