"""Browser facade tools exposed to the LLM-facing registry."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pawmate.core.browser.escalation import CdpAttachFailed, LoginRequired
from pawmate.core.browser.router import BrowserAdapters, BrowserRouter
from pawmate.core.browser.vision_subagent import VisionSubAgent
from pawmate.ocr_vision.service import _image_data_url, get_default_service
from pawmate.tools import native_browser as nb
from pawmate.tools import playwright_browser as pw
from pawmate.tools.registry import APPROVAL_CONFIRM, APPROVAL_NOTIFY, ToolDef, ToolRegistry


async def _ensure_session_with_settings(
    settings: pw.BrowserUseSettings,
    visibility: str,
    *,
    require_attached: bool = False,
) -> None:
    async with pw._browser_lock:
        session = pw._current_session
        if pw._session_is_usable(session):
            if require_attached and getattr(session, "attached", False):
                return
            if not require_attached and not getattr(session, "attached", False):
                if session.visibility == "background" and visibility == "foreground":
                    await pw._close_session(session)
                    pw._current_session = None
                else:
                    return
        pw._current_session = await pw._connect_or_launch(settings, visibility)


def build_adapters() -> BrowserAdapters:
    async def attach_native() -> None:
        settings = replace(
            pw._load_browser_settings(),
            attach_mode="attach",
            cdp_url="http://127.0.0.1:9222",
            profile_directory="native",
        )
        try:
            await _ensure_session_with_settings(settings, "foreground", require_attached=True)
        except (pw.BrowserIntentDowngrade, pw.BrowserDependencyError) as exc:
            error = CdpAttachFailed(str(exc), reason="cdp_attach_failed")
            setattr(error, "diagnostics", getattr(exc, "diagnostics", {}))
            raise error from exc
        session = pw._current_session
        if not getattr(session, "attached", False):
            raise CdpAttachFailed(
                "Current browser session is not a CDP-attached native session.",
                reason="cdp_attach_failed",
            )

    async def launch_managed() -> None:
        settings = replace(
            pw._load_browser_settings(),
            attach_mode="managed",
            cdp_url="",
            profile_directory="managed",
        )
        await _ensure_session_with_settings(settings, "foreground")

    async def navigate(url: str) -> None:
        await pw.browser_navigate(url, mode="foreground")

    async def observe() -> dict:
        return await pw.browser_observe(max_items=80, mode="foreground")

    async def click(ref: str) -> dict:
        return await pw.browser_click(selector=ref, mode="foreground")

    async def type_text(ref: str, value: str) -> dict:
        return await pw.browser_type_text(selector=ref, text=value, mode="foreground")

    async def fill(ref: str, value: str) -> dict:
        return await pw.browser_fill_form(url="", fields={ref: value}, mode="foreground")

    async def extract(query: str) -> dict:
        return await pw.browser_extract_text(max_chars=8000, mode="foreground")

    async def act(payload: dict) -> dict:
        return await pw.browser_input_action(payload)

    return BrowserAdapters(
        attach_native=attach_native,
        launch_managed=launch_managed,
        navigate=navigate,
        observe=observe,
        click=click,
        type_text=type_text,
        fill=fill,
        extract=extract,
        act=act,
    )


def build_vision_subagent() -> VisionSubAgent:
    get_default_service()

    async def screenshot() -> tuple[str, tuple[int, int]]:
        shot = await nb.native_browser_screenshot()
        data_url = _image_data_url(Path(shot["path"]))
        rect = shot["window"]["rect"]
        return data_url, (int(rect["width"]), int(rect["height"]))

    async def locate_fn(data_url: str, target: str):
        from pawmate.core.browser.locate import locate
        from pawmate.core.llm_factory import get_llm_client
        from pawmate.core.llm_router import resolve_llm_route_candidates
        from pawmate.ocr_vision.service import _load_app_config

        async def vision_call(system: str, messages: list, max_tokens: int) -> str:
            choices = resolve_llm_route_candidates("vision", _load_app_config())
            if not choices:
                raise RuntimeError("没有配置可用的视觉模型服务商 API Key")
            choice = choices[0]
            client = get_llm_client(provider=choice.provider, model=choice.model)
            chunks: list[str] = []
            async for event in client.stream(messages=messages, system=system, tools=None, max_tokens=max_tokens):
                if event.get("type") == "text_delta":
                    chunks.append(str(event.get("text", "")))
            return "".join(chunks)

        return await locate(data_url, target, vision_call=vision_call)

    async def click_fn(x: int, y: int) -> None:
        result = await nb.native_browser_click(x=x, y=y, relative=True)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("message") or result.get("error_type") or "native_browser_click failed"))

    return VisionSubAgent(screenshot_fn=screenshot, locate_fn=locate_fn, click_fn=click_fn)


_router: BrowserRouter | None = None


def get_router(goal: str = "") -> BrowserRouter:
    global _router
    if _router is None:
        _router = BrowserRouter(build_adapters(), vision_subagent=build_vision_subagent(), current_goal=goal)
    elif goal:
        _router.set_goal(goal)
    return _router


async def browser_goto(url: str, use_my_login: bool = False, goal: str = "") -> dict:
    try:
        result = await get_router(goal).goto(url, use_my_login=use_my_login)
    except LoginRequired as exc:
        diagnostics = getattr(exc, "diagnostics", {}) or {}
        return {
            "ok": False,
            "operation": "browser_goto",
            "url": url,
            "surface": getattr(exc, "from_surface", "native/dom"),
            "error": "native_profile_mount_failed",
            "error_type": "native_profile_mount_failed",
            "message": (
                "Native browser login-state session could not be mounted. "
                "The browser must expose a reachable CDP remote debugging port."
            ),
            "evidence": getattr(exc, "evidence", str(exc)),
            "diagnostics": diagnostics,
            "suggestion": diagnostics.get("suggestion", {}),
        }
    return {
        "ok": result.ok,
        "operation": "browser_goto",
        "url": result.url,
        "surface": result.surface,
        "reachability": result.reachability,
        "note": result.note,
        "session": pw._browser_session_info(),
    }


async def browser_read() -> dict:
    result = await get_router().read()
    return {
        "ok": result.ok,
        "operation": "browser_read",
        "url": result.url,
        "title": result.title,
        "page_kind": result.page_kind,
        "modality": result.modality,
        "elements": result.elements,
        "note": result.note,
    }


async def browser_act(
    ref: str = "",
    action: str = "click",
    value: str = "",
    intent: str = "",
    key: str = "",
    keys: list | None = None,
    deltaX: int = 0,
    deltaY: int = 0,
    x: int | None = None,
    y: int | None = None,
    space: str = "viewport",
    text: str = "",
    clear: bool = True,
) -> dict:
    return await get_router().act(
        ref=ref,
        action=action,
        value=value,
        intent=intent,
        key=key,
        keys=keys or [],
        deltaX=deltaX,
        deltaY=deltaY,
        x=x,
        y=y,
        space=space,
        text=text,
        clear=clear,
    )


async def browser_extract(query: str = "") -> dict:
    return await get_router().extract(query)


def register_browser_facade_tools(registry: ToolRegistry) -> None:
    tools = [
        ToolDef(
            name="browser_goto",
            description=(
                "Open a URL through the browser router. Set use_my_login=true only when the task requires "
                "the user's logged-in native browser session. Returns reachability for paywalls/login walls."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "use_my_login": {"type": "boolean", "default": False},
                    "goal": {"type": "string", "default": ""},
                },
                "required": ["url"],
            },
            handler=browser_goto,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="browser_read",
            description="Read the current browser page and return actionable elements with refs.",
            input_schema={"type": "object", "properties": {}},
            handler=browser_read,
            approval=APPROVAL_NOTIFY,
        ),
        ToolDef(
            name="browser_act",
            description=(
                "Act on the browser page. In DOM mode pass ref from browser_read plus actions click/type/fill/key/"
                "hotkey/scroll/click_xy/insert_text/smart_type. In vision mode pass intent as the natural-language target."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "default": ""},
                    "action": {
                        "type": "string",
                        "enum": [
                            "click",
                            "type",
                            "fill",
                            "key",
                            "hotkey",
                            "scroll",
                            "click_xy",
                            "insert_text",
                            "smart_type",
                        ],
                        "default": "click",
                    },
                    "value": {"type": "string", "default": ""},
                    "intent": {"type": "string", "default": ""},
                    "key": {
                        "type": "string",
                        "enum": [
                            "",
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
                        ],
                        "default": "",
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "Supported hotkeys include Ctrl+A/C/V/L/R, Alt+Left/Right, and Shift+Tab.",
                    },
                    "deltaX": {"type": "integer", "default": 0},
                    "deltaY": {"type": "integer", "default": 0},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "space": {"type": "string", "enum": ["viewport", "screen"], "default": "viewport"},
                    "text": {"type": "string", "default": ""},
                    "clear": {"type": "boolean", "default": True},
                },
            },
            handler=browser_act,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="browser_extract",
            description="Extract readable text from the current browser page.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "default": ""}},
            },
            handler=browser_extract,
            approval=APPROVAL_NOTIFY,
        ),
    ]
    registry.register_bulk({tool.name: tool for tool in tools})
