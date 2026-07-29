"""Browser input runtime backends.

This module keeps browser_act input mechanics out of the facade/router layer.
Backends are tried in priority order and return a uniform fallback trace.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger("pawmate.browser.input")


SUPPORTED_KEYS = {
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "Delete",
    "Space",
    "PageUp",
    "PageDown",
    "Home",
    "End",
}

_KEY_ALIASES = {
    "esc": "Escape",
    "return": "Enter",
    "spacebar": "Space",
    " ": "Space",
    "del": "Delete",
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "shift": "Shift",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "option": "Alt",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
}

_SUPPORTED_HOTKEYS = {
    ("Control", "A"),
    ("Control", "C"),
    ("Control", "V"),
    ("Control", "L"),
    ("Control", "R"),
    ("Alt", "ArrowLeft"),
    ("Alt", "ArrowRight"),
    ("Shift", "Tab"),
}

_CDP_KEY_DEFS = {
    "ArrowUp": ("ArrowUp", "ArrowUp", 38),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39),
    "Enter": ("Enter", "Enter", 13),
    "Escape": ("Escape", "Escape", 27),
    "Tab": ("Tab", "Tab", 9),
    "Backspace": ("Backspace", "Backspace", 8),
    "Delete": ("Delete", "Delete", 46),
    "Space": (" ", "Space", 32),
    "PageUp": ("PageUp", "PageUp", 33),
    "PageDown": ("PageDown", "PageDown", 34),
    "Home": ("Home", "Home", 36),
    "End": ("End", "End", 35),
    "Control": ("Control", "ControlLeft", 17),
    "Shift": ("Shift", "ShiftLeft", 16),
    "Alt": ("Alt", "AltLeft", 18),
    "Meta": ("Meta", "MetaLeft", 91),
}

_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

_HUMAN_VERIFICATION_KEYWORDS = (
    ("sms_verification_detected", ("短信验证码", "发送到手机号", "sms verification", "sms code")),
    ("security_verification_detected", ("安全验证", "security check", "suspicious activity")),
    ("human_verification_detected", ("人机验证", "human verification")),
    ("captcha_detected", ("验证码", "请输入验证码", "captcha", "verification code")),
)


class BackendUnsupported(RuntimeError):
    """Raised when a backend cannot safely handle an action."""


@dataclass
class BrowserActionContext:
    session: Any = None
    page: Any = None
    allow_os_input: bool = False
    security_service: Any = None


def normalize_key(key: str) -> str:
    raw = str(key or "").strip()
    if not raw:
        raise ValueError("key is required")
    lowered = raw.lower().replace("_", "").replace("-", "")
    if lowered in _KEY_ALIASES:
        return _KEY_ALIASES[lowered]
    if len(raw) == 1 and raw.isalpha():
        return raw.upper()
    if len(raw) == 1 and raw.isdigit():
        return raw
    return raw[0].upper() + raw[1:] if raw[:1].islower() else raw


def normalize_hotkey(keys: Any) -> list[str]:
    if isinstance(keys, str):
        parts = [part for part in keys.replace("+", " ").split(" ") if part]
    elif isinstance(keys, (list, tuple)):
        parts = list(keys)
    else:
        raise ValueError("keys must be a list or + separated string")
    normalized = [normalize_key(str(part)) for part in parts]
    combo = tuple(normalized)
    if combo not in _SUPPORTED_HOTKEYS:
        raise ValueError(f"unsupported hotkey: {'+'.join(normalized)}")
    return normalized


def validate_key(key: str) -> str:
    normalized = normalize_key(key)
    if normalized not in SUPPORTED_KEYS:
        raise ValueError(f"unsupported key: {key}")
    return normalized


def normalize_action_payload(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "click").strip().lower()
    normalized = dict(payload)
    normalized["action"] = action

    if action in {"click", "type", "fill", "scroll", "smart_type"}:
        normalized["ref"] = str(payload.get("ref") or "").strip()
    if action in {"type", "fill", "insert_text", "smart_type"}:
        normalized["text"] = str(payload.get("text") if payload.get("text") is not None else payload.get("value") or "")
    if action == "key":
        normalized["key"] = validate_key(str(payload.get("key") or payload.get("value") or ""))
    elif action == "hotkey":
        normalized["keys"] = normalize_hotkey(payload.get("keys") or payload.get("value") or "")
    elif action == "scroll":
        normalized["deltaX"] = _as_number(payload.get("deltaX"), 0)
        normalized["deltaY"] = _as_number(payload.get("deltaY"), payload.get("amount", 0) or 0)
    elif action == "click_xy":
        normalized["x"] = _as_number(payload.get("x"), None)
        normalized["y"] = _as_number(payload.get("y"), None)
        if normalized["x"] is None or normalized["y"] is None:
            raise ValueError("x and y are required for click_xy")
        normalized["space"] = str(payload.get("space") or "viewport").strip().lower()
    elif action == "smart_type":
        normalized["clear"] = bool(payload.get("clear", True))

    allowed = {"click", "type", "fill", "key", "hotkey", "scroll", "click_xy", "insert_text", "smart_type"}
    if action not in allowed:
        raise ValueError(f"unsupported action: {action}")
    return normalized


def _as_number(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, float):
            return value
        return int(value)
    except Exception:
        return default


def _ok(action: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "action": action, **extra}


def _reason(result: Any) -> str:
    if isinstance(result, dict):
        return str(
            result.get("reason")
            or result.get("message")
            or result.get("error")
            or result.get("error_type")
            or "backend failed"
        )
    return str(result or "backend failed")


async def detect_human_verification(page: Any) -> dict[str, Any] | None:
    """Return a human handoff result when the page shows verification copy."""
    if page is None:
        return None
    try:
        text = await page.evaluate(
            """
            () => {
                const pieces = [];
                if (document.body) pieces.push(document.body.innerText || document.body.textContent || '');
                for (const el of document.querySelectorAll('input, textarea, [aria-label], [placeholder], [title]')) {
                    pieces.push(el.getAttribute('aria-label') || '');
                    pieces.push(el.getAttribute('placeholder') || '');
                    pieces.push(el.getAttribute('title') || '');
                    pieces.push(el.value || '');
                }
                return pieces.join('\\n').slice(0, 20000);
            }
            """
        )
    except Exception:
        return None

    haystack = str(text or "").casefold()
    for reason, keywords in _HUMAN_VERIFICATION_KEYWORDS:
        for keyword in keywords:
            if keyword.casefold() in haystack:
                result = {
                    "ok": False,
                    "error": "human_handoff_required",
                    "error_type": "human_handoff_required",
                    "reason": reason,
                    "message": _handoff_message(reason),
                    "can_resume": True,
                    "event": {
                        "type": "human_handoff_required",
                        "reason": reason,
                    },
                }
                _publish_handoff_event(result)
                return result
    return None


def _handoff_message(reason: str) -> str:
    if reason == "sms_verification_detected":
        return "The page is asking for SMS verification. Human handoff is required."
    if reason == "security_verification_detected":
        return "The page is showing a security verification. Human handoff is required."
    if reason == "captcha_detected":
        return "The page is showing a captcha or verification code. Human handoff is required."
    return "The page requires human verification. Human handoff is required."


def _publish_handoff_event(result: dict[str, Any]) -> None:
    try:
        from pawmate.bridge.contracts import ToolNotifyEvent
        from pawmate.bridge.event_bus import event_bus

        event_bus.publish(ToolNotifyEvent("browser_human_handoff", json.dumps(result, ensure_ascii=False)))
    except Exception:
        logger.debug("[BrowserInput] human handoff event publish failed", exc_info=True)


class BrowserInputBackend:
    backend_name = "base"

    def can_handle(self, context: BrowserActionContext) -> bool:
        return False

    async def click_ref(self, context: BrowserActionContext, ref: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise BackendUnsupported("click_ref unsupported")

    async def click_xy(
        self,
        context: BrowserActionContext,
        x: float,
        y: float,
        space: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise BackendUnsupported("click_xy unsupported")

    async def type_text(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise BackendUnsupported("type_text unsupported")

    async def fill_text(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.type_text(context, ref, text, {**payload, "clear": True})

    async def insert_text(self, context: BrowserActionContext, text: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise BackendUnsupported("insert_text unsupported")

    async def smart_type(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        clear: bool,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise BackendUnsupported("smart_type unsupported")

    async def press_key(self, context: BrowserActionContext, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise BackendUnsupported("press_key unsupported")

    async def press_hotkey(self, context: BrowserActionContext, keys: list[str], payload: dict[str, Any]) -> dict[str, Any]:
        raise BackendUnsupported("press_hotkey unsupported")

    async def scroll(
        self,
        context: BrowserActionContext,
        delta_x: float,
        delta_y: float,
        ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise BackendUnsupported("scroll unsupported")


class PlaywrightInputBackend(BrowserInputBackend):
    backend_name = "playwright"

    def can_handle(self, context: BrowserActionContext) -> bool:
        page = context.page
        if page is None:
            return False
        try:
            return not bool(page.is_closed())
        except Exception:
            return True

    async def click_ref(self, context: BrowserActionContext, ref: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not ref:
            raise BackendUnsupported("ref is required for click")
        await context.page.wait_for_selector(ref, timeout=2500)
        await context.page.click(ref)
        return _ok("click", ref=ref)

    async def click_xy(
        self,
        context: BrowserActionContext,
        x: float,
        y: float,
        space: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if space != "viewport":
            raise BackendUnsupported("playwright only supports viewport coordinates")
        mouse = context.page.mouse
        await mouse.move(x, y)
        await mouse.down()
        await mouse.up()
        return _ok("click_xy", x=x, y=y, space=space)

    async def type_text(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if ref:
            await context.page.wait_for_selector(ref, timeout=2500)
            await context.page.click(ref)
        if payload.get("clear", True):
            await _keyboard_press(context.page, "Control+A")
            await _keyboard_press(context.page, "Backspace")
        await _keyboard_insert_text(context.page, text)
        observed = await _read_focused_text(context.page, ref)
        return _ok("type", ref=ref, text_length=len(text), after_text=observed.get("text", ""))

    async def fill_text(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not ref:
            raise BackendUnsupported("ref is required for fill")
        await context.page.wait_for_selector(ref, timeout=2500)
        await context.page.fill(ref, text)
        observed = await _read_focused_text(context.page, ref)
        return _ok("fill", ref=ref, text_length=len(text), after_text=observed.get("text", ""))

    async def insert_text(self, context: BrowserActionContext, text: str, payload: dict[str, Any]) -> dict[str, Any]:
        before = await _read_focused_text(context.page, "")
        await _keyboard_insert_text(context.page, text)
        await _dispatch_editor_events(context.page, "")
        after = await _read_focused_text(context.page, "")
        changed = before.get("text") != after.get("text") or text in after.get("text", "")
        return _ok("insert_text", text_length=len(text), changed=changed, after_text=after.get("text", ""))

    async def smart_type(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        clear: bool,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if ref:
            await context.page.wait_for_selector(ref, timeout=2500)
            await context.page.click(ref)
        if clear:
            await _keyboard_press(context.page, "Control+A")
            await _keyboard_press(context.page, "Backspace")
        await _keyboard_insert_text(context.page, text)
        await _dispatch_editor_events(context.page, ref)
        observed = await _read_focused_text(context.page, ref)
        after_text = observed.get("text", "")
        return _ok(
            "smart_type",
            ref=ref,
            text_length=len(text),
            success=text in after_text,
            after_text=after_text,
            submit_ready=bool(observed.get("submit_ready")),
        )

    async def press_key(self, context: BrowserActionContext, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        await _keyboard_press(context.page, key)
        return _ok("key", key=key)

    async def press_hotkey(self, context: BrowserActionContext, keys: list[str], payload: dict[str, Any]) -> dict[str, Any]:
        combo = "+".join(keys)
        await _keyboard_press(context.page, combo)
        return _ok("hotkey", keys=keys)

    async def scroll(
        self,
        context: BrowserActionContext,
        delta_x: float,
        delta_y: float,
        ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if ref:
            await context.page.wait_for_selector(ref, timeout=2500)
            await context.page.hover(ref)
        await context.page.mouse.wheel(delta_x, delta_y)
        return _ok("scroll", ref=ref, deltaX=delta_x, deltaY=delta_y)


class CDPInputBackend(BrowserInputBackend):
    backend_name = "cdp"

    def can_handle(self, context: BrowserActionContext) -> bool:
        page = context.page
        if page is None:
            return False
        page_context = getattr(page, "context", None)
        return bool(page_context and hasattr(page_context, "new_cdp_session"))

    async def _client(self, context: BrowserActionContext) -> Any:
        return await context.page.context.new_cdp_session(context.page)

    async def _send(self, context: BrowserActionContext, method: str, params: dict[str, Any] | None = None) -> Any:
        client = await self._client(context)
        return await client.send(method, params or {})

    async def click_ref(self, context: BrowserActionContext, ref: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not ref:
            raise BackendUnsupported("ref is required for click")
        point = await _element_center(context.page, ref)
        return await self.click_xy(context, point["x"], point["y"], "viewport", payload)

    async def click_xy(
        self,
        context: BrowserActionContext,
        x: float,
        y: float,
        space: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if space != "viewport":
            raise BackendUnsupported("cdp only supports viewport coordinates")
        await self._send(context, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self._send(
            context,
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await self._send(
            context,
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        return _ok("click_xy", x=x, y=y, space=space)

    async def type_text(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if ref:
            await _focus_element(context.page, ref)
        if payload.get("clear", True):
            await self.press_hotkey(context, ["Control", "A"], payload)
            await self.press_key(context, "Backspace", payload)
        await self.insert_text(context, text, payload)
        observed = await _read_focused_text(context.page, ref)
        return _ok("type", ref=ref, text_length=len(text), after_text=observed.get("text", ""))

    async def insert_text(self, context: BrowserActionContext, text: str, payload: dict[str, Any]) -> dict[str, Any]:
        before = await _read_focused_text(context.page, "")
        await self._send(context, "Input.insertText", {"text": text})
        await _dispatch_editor_events(context.page, "")
        after = await _read_focused_text(context.page, "")
        changed = before.get("text") != after.get("text") or text in after.get("text", "")
        return _ok("insert_text", text_length=len(text), changed=changed, after_text=after.get("text", ""))

    async def smart_type(
        self,
        context: BrowserActionContext,
        ref: str,
        text: str,
        clear: bool,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if ref:
            await _focus_element(context.page, ref)
        if clear:
            await self.press_hotkey(context, ["Control", "A"], payload)
            await self.press_key(context, "Backspace", payload)
        await self.insert_text(context, text, payload)
        await _dispatch_editor_events(context.page, ref)
        observed = await _read_focused_text(context.page, ref)
        after_text = observed.get("text", "")
        return _ok(
            "smart_type",
            ref=ref,
            text_length=len(text),
            success=text in after_text,
            after_text=after_text,
            submit_ready=bool(observed.get("submit_ready")),
        )

    async def press_key(self, context: BrowserActionContext, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self._dispatch_key(context, key, "rawKeyDown")
        await self._dispatch_key(context, key, "keyUp")
        return _ok("key", key=key)

    async def press_hotkey(self, context: BrowserActionContext, keys: list[str], payload: dict[str, Any]) -> dict[str, Any]:
        modifiers = [key for key in keys[:-1] if key in _MODIFIER_BITS]
        final_key = keys[-1]
        modifier_mask = _modifier_mask(modifiers)
        for key in modifiers:
            await self._dispatch_key(context, key, "rawKeyDown", modifiers=modifier_mask)
        await self._dispatch_key(context, final_key, "rawKeyDown", modifiers=modifier_mask)
        await self._dispatch_key(context, final_key, "keyUp", modifiers=modifier_mask)
        for key in reversed(modifiers):
            await self._dispatch_key(context, key, "keyUp", modifiers=modifier_mask)
        return _ok("hotkey", keys=keys)

    async def scroll(
        self,
        context: BrowserActionContext,
        delta_x: float,
        delta_y: float,
        ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if ref:
            point = await _element_center(context.page, ref)
        else:
            point = await context.page.evaluate(
                "() => ({ x: Math.floor(window.innerWidth / 2), y: Math.floor(window.innerHeight / 2) })"
            )
        await self._send(
            context,
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": point["x"], "y": point["y"], "deltaX": delta_x, "deltaY": delta_y},
        )
        return _ok("scroll", ref=ref, deltaX=delta_x, deltaY=delta_y)

    async def _dispatch_key(
        self,
        context: BrowserActionContext,
        key: str,
        event_type: str,
        *,
        modifiers: int = 0,
    ) -> None:
        key_def = _cdp_key_definition(key)
        params = {
            "type": event_type,
            "key": key_def["key"],
            "code": key_def["code"],
            "windowsVirtualKeyCode": key_def["vk"],
            "nativeVirtualKeyCode": key_def["vk"],
            "modifiers": modifiers,
        }
        if event_type != "keyUp" and key_def.get("text") and not modifiers:
            params["text"] = key_def["text"]
            params["unmodifiedText"] = key_def["text"]
        await self._send(context, "Input.dispatchKeyEvent", params)


class OSInputBackend(BrowserInputBackend):
    backend_name = "os"

    def can_handle(self, context: BrowserActionContext) -> bool:
        return bool(context.allow_os_input and sys.platform == "win32")

    async def click_xy(
        self,
        context: BrowserActionContext,
        x: float,
        y: float,
        space: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not context.allow_os_input:
            raise BackendUnsupported("os input requires explicit approval")
        if sys.platform != "win32":
            raise BackendUnsupported(f"os input unsupported on {platform.system()}")
        from pawmate.tools.browser import native as native_browser

        relative = space != "screen"
        result = await native_browser.native_browser_click(int(x), int(y), relative=relative)
        if not result.get("ok"):
            raise BackendUnsupported(_reason(result))
        return _ok("click_xy", x=x, y=y, space=space, native=result)

    async def insert_text(self, context: BrowserActionContext, text: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not context.allow_os_input:
            raise BackendUnsupported("os input requires explicit approval")
        if sys.platform != "win32":
            raise BackendUnsupported(f"os input unsupported on {platform.system()}")
        from pawmate.tools.browser import native as native_browser

        result = await native_browser.native_browser_type_text(text)
        if not result.get("ok"):
            raise BackendUnsupported(_reason(result))
        return _ok("insert_text", text_length=len(text), native=result)

    async def press_key(self, context: BrowserActionContext, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.press_hotkey(context, [key], payload)

    async def press_hotkey(self, context: BrowserActionContext, keys: list[str], payload: dict[str, Any]) -> dict[str, Any]:
        if not context.allow_os_input:
            raise BackendUnsupported("os input requires explicit approval")
        if sys.platform != "win32":
            raise BackendUnsupported(f"os input unsupported on {platform.system()}")
        from pawmate.tools.browser import native as native_browser

        result = await native_browser.native_browser_hotkey("+".join(keys))
        if not result.get("ok"):
            raise BackendUnsupported(_reason(result))
        return _ok("hotkey", keys=keys, native=result)


class BrowserActionExecutor:
    def __init__(self, backends: list[BrowserInputBackend] | None = None) -> None:
        self.backends = backends or [PlaywrightInputBackend(), CDPInputBackend(), OSInputBackend()]

    async def execute(self, context: BrowserActionContext, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            normalized = normalize_action_payload(payload)
        except Exception as exc:
            return {
                "ok": False,
                "operation": "browser_act",
                "error_type": "bad_args",
                "error": "bad_args",
                "message": str(exc),
                "action": str(payload.get("action") or ""),
            }

        handoff = await detect_human_verification(context.page)
        if handoff is not None:
            return {"operation": "browser_act", "action": normalized["action"], **handoff}

        fallbacks: list[dict[str, Any]] = []
        for backend in self.backends:
            if not backend.can_handle(context):
                fallbacks.append(
                    {
                        "backend": backend.backend_name,
                        "ok": False,
                        "reason": "backend cannot handle current context",
                    }
                )
                continue
            try:
                result = await self._execute_backend(backend, context, normalized)
            except BackendUnsupported as exc:
                fallbacks.append({"backend": backend.backend_name, "ok": False, "reason": str(exc)})
                continue
            except Exception as exc:
                logger.debug("[BrowserInput] backend failed: %s", backend.backend_name, exc_info=True)
                fallbacks.append({"backend": backend.backend_name, "ok": False, "reason": str(exc)})
                continue
            if result.get("ok"):
                return {
                    "operation": "browser_act",
                    **result,
                    "backend": backend.backend_name,
                    "fallbacks": fallbacks,
                }
            fallbacks.append({"backend": backend.backend_name, "ok": False, "reason": _reason(result)})

        return {
            "ok": False,
            "operation": "browser_act",
            "action": normalized["action"],
            "backend": None,
            "fallbacks": fallbacks,
            "error": "all_backends_failed",
            "error_type": "all_backends_failed",
            "message": fallbacks[-1]["reason"] if fallbacks else "no input backend available",
        }

    async def _execute_backend(
        self,
        backend: BrowserInputBackend,
        context: BrowserActionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        action = payload["action"]
        if action == "click":
            return await backend.click_ref(context, payload.get("ref", ""), payload)
        if action == "type":
            return await backend.type_text(context, payload.get("ref", ""), payload.get("text", ""), payload)
        if action == "fill":
            return await backend.fill_text(context, payload.get("ref", ""), payload.get("text", ""), payload)
        if action == "key":
            return await backend.press_key(context, payload["key"], payload)
        if action == "hotkey":
            return await backend.press_hotkey(context, payload["keys"], payload)
        if action == "scroll":
            return await backend.scroll(context, payload["deltaX"], payload["deltaY"], payload.get("ref", ""), payload)
        if action == "click_xy":
            return await backend.click_xy(context, payload["x"], payload["y"], payload.get("space", "viewport"), payload)
        if action == "insert_text":
            return await backend.insert_text(context, payload.get("text", ""), payload)
        if action == "smart_type":
            return await backend.smart_type(
                context,
                payload.get("ref", ""),
                payload.get("text", ""),
                bool(payload.get("clear", True)),
                payload,
            )
        raise BackendUnsupported(f"unsupported action: {action}")


async def _keyboard_press(page: Any, key: str) -> None:
    keyboard = page.keyboard
    if hasattr(keyboard, "press"):
        await keyboard.press(key)
        return
    raise BackendUnsupported("keyboard.press unavailable")


async def _keyboard_insert_text(page: Any, text: str) -> None:
    keyboard = page.keyboard
    if hasattr(keyboard, "insert_text"):
        await keyboard.insert_text(text)
        return
    if hasattr(keyboard, "type"):
        await keyboard.type(text)
        return
    raise BackendUnsupported("keyboard text input unavailable")


async def _read_focused_text(page: Any, ref: str) -> dict[str, Any]:
    return await page.evaluate(
        """
        (selector) => {
            const el = selector ? document.querySelector(selector) : document.activeElement;
            if (!el) return { found: false, text: '', submit_ready: false };
            const value = ('value' in el) ? String(el.value || '') : '';
            const text = value || String(el.textContent || '');
            const submitReady = Array.from(document.querySelectorAll(
                'button, input[type="submit"], [role="button"]'
            )).some((node) => {
                const style = getComputedStyle(node);
                return !node.disabled && node.getAttribute('aria-disabled') !== 'true' &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            });
            return {
                found: true,
                tag: el.tagName ? el.tagName.toLowerCase() : '',
                text,
                value,
                submit_ready: submitReady
            };
        }
        """,
        ref or "",
    )


async def _dispatch_editor_events(page: Any, ref: str) -> None:
    try:
        await page.evaluate(
            """
            (selector) => {
                const el = selector ? document.querySelector(selector) : document.activeElement;
                if (!el) return false;
                const opts = { bubbles: true, cancelable: true, inputType: 'insertText', data: null };
                try { el.dispatchEvent(new InputEvent('beforeinput', opts)); } catch (_) {}
                try { el.dispatchEvent(new InputEvent('input', opts)); } catch (_) {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
                document.dispatchEvent(new Event('selectionchange', { bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                return true;
            }
            """,
            ref or "",
        )
    except Exception:
        logger.debug("[BrowserInput] dispatch editor events failed", exc_info=True)


async def _focus_element(page: Any, ref: str) -> None:
    focused = await page.evaluate(
        """
        (selector) => {
            const el = document.querySelector(selector);
            if (!el) return false;
            el.focus();
            return document.activeElement === el;
        }
        """,
        ref,
    )
    if not focused:
        raise BackendUnsupported(f"element not focusable: {ref}")


async def _element_center(page: Any, ref: str) -> dict[str, float]:
    point = await page.evaluate(
        """
        (selector) => {
            const el = document.querySelector(selector);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }
        """,
        ref,
    )
    if not point:
        raise BackendUnsupported(f"element not found or not visible: {ref}")
    return {"x": float(point["x"]), "y": float(point["y"])}


def _modifier_mask(keys: list[str]) -> int:
    mask = 0
    for key in keys:
        mask |= _MODIFIER_BITS.get(key, 0)
    return mask


def _cdp_key_definition(key: str) -> dict[str, Any]:
    if key in _CDP_KEY_DEFS:
        cdp_key, code, vk = _CDP_KEY_DEFS[key]
        text = " " if key == "Space" else ""
        return {"key": cdp_key, "code": code, "vk": vk, "text": text}
    normalized = normalize_key(key)
    if len(normalized) == 1:
        char = normalized.upper()
        return {"key": char.lower(), "code": f"Key{char}", "vk": ord(char), "text": ""}
    raise BackendUnsupported(f"cdp key unsupported: {key}")
