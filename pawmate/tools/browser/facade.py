"""Browser facade tools exposed to the LLM-facing registry."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

from pawmate.core.browser.escalation import CdpAttachFailed, LoginRequired
from pawmate.core.browser.boundary import (
    BrowserActionType,
    BrowserRisk,
    annotate_browser_result,
    classify_browser_action,
    is_logged_in_sensitive,
)
from pawmate.core.browser.router import BrowserAdapters, BrowserRouter
from pawmate.core.browser.vision_subagent import VisionSubAgent
from pawmate.ocr_vision.service import _image_data_url, get_default_service
from pawmate.storage.app_paths import get_app_paths
from pawmate.tools.browser import native as nb
from pawmate.tools.browser import playwright_runtime as pw
from pawmate.tools.core.registry import (
    APPROVAL_CONFIRM,
    APPROVAL_NOTIFY,
    RiskLevel,
    SideEffectLevel,
    ToolCategory,
    ToolDef,
    ToolRegistry,
)


async def _ensure_session_with_settings(
    settings: pw.BrowserUseSettings,
    visibility: str,
    *,
    require_attached: bool = False,
) -> None:
    async with pw._get_browser_lock():
        slot = pw._session_slot(visibility, native=require_attached)
        session = pw._browser_sessions.get(slot)
        if pw._session_is_usable(session):
            if require_attached and not getattr(session, "attached", False):
                pw._forget_session(slot=slot)
            else:
                pw._current_session = session
                return

        # A CDP-attached session is valuable state. Do not replace it with a
        # normal managed session just because the next operation is read/navigate.
        current = pw._current_session
        if pw._session_is_usable(current):
            if require_attached:
                if getattr(current, "attached", False):
                    pw._remember_session(current, slot=slot)
                    return
            elif getattr(current, "attached", False) and getattr(current, "visibility", "") == visibility:
                pw._remember_session(current, slot=slot)
                return
            elif getattr(current, "visibility", "") == visibility and not getattr(current, "attached", False):
                pw._remember_session(current, slot=slot)
                return

        if session is not None and not getattr(session, "attached", False):
            await pw._close_session(session)
            pw._forget_session(slot=slot)
        pw._remember_session(await pw._connect_or_launch(settings, visibility), slot=slot)


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

    async def launch_managed(visibility: str = "auto", goal: str = "", url: str = "") -> str:
        resolved_visibility = pw._classify_visibility(mode=visibility, purpose=goal, url=url)
        base = pw._load_browser_settings()

        if resolved_visibility == "background":
            # Silent/search/read-only tasks must not steal focus or open a visible
            # browser. Use an isolated headless Playwright session for these.
            settings = replace(
                base,
                attach_mode="managed",
                profile_directory="none",
                headless=True,
            )
        else:
            # Foreground non-login browsing should be a real Edge/Chrome process
            # launched with a CDP port and a PawMate-owned persistent profile.
            # This avoids the Playwright-managed Chromium window that can trigger
            # --no-sandbox warnings and broken site rendering.
            # Keep attach_mode=auto as auto. Do not upgrade auto -> prefer here,
            # otherwise foreground browsing starts probing arbitrary 127.0.0.1:9222
            # again and may attach to a browser PawMate does not own.
            settings = replace(
                base,
                attach_mode=base.attach_mode or "auto",
                profile_directory=base.profile_directory or "auto",
            )
        await _ensure_session_with_settings(settings, resolved_visibility)
        return resolved_visibility

    async def navigate(url: str, visibility: str = "auto", goal: str = "", require_native: bool = False) -> None:
        await pw.browser_navigate(url, mode=visibility, purpose=goal, require_native=require_native)

    async def observe(visibility: str = "auto", goal: str = "", require_native: bool = False) -> dict:
        return await pw.browser_observe(max_items=80, mode=visibility, purpose=goal, require_native=require_native)

    async def click(ref: str) -> dict:
        return await pw.browser_click(selector=ref, mode="foreground")

    async def type_text(ref: str, value: str) -> dict:
        return await pw.browser_type_text(selector=ref, text=value, mode="foreground")

    async def fill(ref: str, value: str) -> dict:
        return await pw.browser_fill_form(url="", fields={ref: value}, mode="foreground")

    async def extract(query: str, goal: str = "", visibility: str = "auto", require_native: bool = False) -> dict:
        return await pw.browser_extract_text(max_chars=8000, mode=visibility, purpose=query or goal, require_native=require_native)

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
    click_context: dict[str, object] = {"kind": "", "page": None, "window_id": 0}

    async def screenshot() -> tuple[str, tuple[int, int]]:
        # Prefer the current Playwright/CDP page. Falling back to native desktop
        # screenshot can capture the wrong browser/window when background or CDP
        # sessions are active.
        session = getattr(pw, "_current_session", None)
        page = getattr(session, "page", None) if pw._session_is_usable(session) else None
        if page is not None:
            try:
                target = get_app_paths().downloads_dir / f"browser_page_{int(time.time())}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(target), full_page=False)
                viewport = page.viewport_size or {}
                width = int(viewport.get("width") or await page.evaluate("() => window.innerWidth"))
                height = int(viewport.get("height") or await page.evaluate("() => window.innerHeight"))
                click_context.update(kind="playwright", page=page, window_id=0)
                return _image_data_url(target), (width, height)
            except Exception:
                pass

        shot = await nb.native_browser_screenshot()
        data_url = _image_data_url(Path(shot["path"]))
        rect = shot["window"]["rect"]
        click_context.update(kind="native", page=None, window_id=int(shot["window"]["id"]))
        return data_url, (int(rect["width"]), int(rect["height"]))

    async def locate_fn(data_url: str, target: str, image_size: tuple[int, int]):
        from pawmate.core.browser.locate import locate
        from pawmate.core.model.llm_factory import get_llm_client
        from pawmate.core.model.llm_router import resolve_llm_route_candidates
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

        return await locate(data_url, target, vision_call=vision_call, image_size=image_size, min_confidence=0.75)

    async def click_fn(x: int, y: int) -> None:
        page = click_context.get("page")
        if click_context.get("kind") == "playwright" and page is not None:
            if not pw._page_is_closed(page):
                await page.mouse.click(x, y)
                return
            raise RuntimeError("page used for visual location was closed before click")

        result = await nb.native_browser_click(
            x=x,
            y=y,
            window_id=int(click_context.get("window_id") or 0),
            relative=True,
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("message") or result.get("error_type") or "native_browser_click failed"))

    return VisionSubAgent(screenshot_fn=screenshot, locate_fn=locate_fn, click_fn=click_fn)


_router: BrowserRouter | None = None


def get_router(goal: str = "") -> BrowserRouter:
    global _router
    if _router is None:
        _router = BrowserRouter(build_adapters(), vision_subagent=build_vision_subagent(), current_goal=goal or "")
    else:
        _router.set_goal(goal or "")
    return _router


async def browser_goto(
    url: str,
    use_my_login: bool = False,
    goal: str = "",
    visibility: str = "auto",
) -> dict:
    try:
        result = await get_router(goal).goto(
            url,
            use_my_login=use_my_login,
            visibility=visibility,
        )
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
            "recoverable": True,
            "recovery": {
                "action": "retry_managed_session",
                "instruction": "If the user did not explicitly require their existing login session, retry browser_goto with use_my_login=false.",
            },
        }
    except pw.BrowserDependencyError as exc:
        error_type = "native_session_disconnected" if use_my_login or "native" in str(exc) else "browser_dependency_error"
        return {
            "ok": False,
            "operation": "browser_goto",
            "url": url,
            "error": error_type,
            "error_type": error_type,
            "message": str(exc),
            "session": pw._browser_session_info(),
        }
    response = {
        "ok": result.ok,
        "operation": "browser_goto",
        "url": result.url,
        "surface": result.surface,
        "reachability": result.reachability,
        "note": result.note,
        "visibility": getattr(result, "visibility", visibility),
        "require_native": getattr(result, "require_native", use_my_login),
        "session": pw._browser_session_info(),
    }
    return annotate_browser_result(
        response,
        action=BrowserActionType.NAVIGATE,
        risk=BrowserRisk.MEDIUM if use_my_login else BrowserRisk.LOW,
        backend=str(result.surface or "browser_router"),
        sensitive=is_logged_in_sensitive(
            use_my_login=use_my_login,
            surface=str(result.surface or ""),
            session=response.get("session"),
        ),
        untrusted_observation=False,
    )


async def browser_read(visibility: str = "auto", goal: str = "") -> dict:
    try:
        result = await get_router(goal).read(visibility=visibility)
    except pw.BrowserDependencyError as exc:
        error_type = "native_session_disconnected" if "native" in str(exc) else "browser_dependency_error"
        return annotate_browser_result(
            {
                "ok": False,
                "operation": "browser_read",
                "error": error_type,
                "error_type": error_type,
                "message": str(exc),
                "session": pw._browser_session_info(),
            },
            action=BrowserActionType.READ,
            risk=BrowserRisk.LOW,
            backend="browser_router",
            sensitive=False,
            untrusted_observation=True,
        )
    response = {
        "ok": result.ok,
        "operation": "browser_read",
        "url": result.url,
        "title": result.title,
        "page_kind": result.page_kind,
        "modality": result.modality,
        "elements": result.elements,
        "note": result.note,
        "visibility": getattr(result, "visibility", visibility),
        "require_native": getattr(result, "require_native", False),
        "session": pw._browser_session_info(),
    }
    return annotate_browser_result(
        response,
        action=BrowserActionType.READ,
        risk=BrowserRisk.LOW,
        backend=str(result.modality or "browser_router"),
        sensitive=False,
        untrusted_observation=True,
    )


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
    visibility: str = "auto",
) -> dict:
    action_type, risk = classify_browser_action(
        action,
        intent=intent,
        text=text or value,
        key=key,
        x=x,
        y=y,
    )
    result = await get_router(intent or "").act(
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
        visibility=visibility,
    )
    return annotate_browser_result(
        result if isinstance(result, dict) else {"ok": True, "result": result},
        action=action_type,
        risk=risk,
        backend=str((result or {}).get("backend") or (result or {}).get("modality") or "browser_router")
        if isinstance(result, dict)
        else "browser_router",
        fallback=str((result or {}).get("fallback") or "") if isinstance(result, dict) else "",
        sensitive=False,
        untrusted_observation=False,
    )


async def browser_extract(query: str = "", visibility: str = "auto") -> dict:
    try:
        result = await get_router(query).extract(query, visibility=visibility)
    except pw.BrowserDependencyError as exc:
        error_type = "native_session_disconnected" if "native" in str(exc) else "browser_dependency_error"
        return annotate_browser_result(
            {
                "ok": False,
                "operation": "browser_extract",
                "error": error_type,
                "error_type": error_type,
                "message": str(exc),
                "session": pw._browser_session_info(),
            },
            action=BrowserActionType.EXTRACT,
            risk=BrowserRisk.LOW,
            backend="browser_router",
            sensitive=False,
            untrusted_observation=True,
        )
    return annotate_browser_result(
        result if isinstance(result, dict) else {"ok": True, "content": result},
        action=BrowserActionType.EXTRACT,
        risk=BrowserRisk.LOW,
        backend=str((result or {}).get("modality") or "browser_router") if isinstance(result, dict) else "browser_router",
        sensitive=False,
        untrusted_observation=True,
    )


def _browser_act_runtime_policy(input_data: dict) -> dict[str, str]:
    _action, risk = classify_browser_action(
        str(input_data.get("action") or "click"),
        intent=str(input_data.get("intent") or ""),
        text=str(input_data.get("text") or input_data.get("value") or ""),
        key=str(input_data.get("key") or ""),
        x=input_data.get("x"),
        y=input_data.get("y"),
    )
    if risk == BrowserRisk.LOW:
        return {
            "risk": RiskLevel.LOW.value,
            "side_effect": SideEffectLevel.READ_ONLY.value,
            "approval": APPROVAL_NOTIFY,
        }
    if risk == BrowserRisk.CRITICAL:
        return {
            "risk": RiskLevel.CRITICAL.value,
            "side_effect": SideEffectLevel.EXTERNAL_WRITE.value,
            "approval": APPROVAL_CONFIRM,
        }
    return {
        "risk": RiskLevel.HIGH.value if risk == BrowserRisk.HIGH else RiskLevel.MEDIUM.value,
        "side_effect": SideEffectLevel.EXTERNAL_WRITE.value,
        "approval": APPROVAL_CONFIRM,
    }


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
                    "visibility": {
                        "type": "string",
                        "enum": ["auto", "background", "foreground"],
                        "default": "auto",
                        "description": "Use background for silent search/read tasks; foreground only when the user must see or interact with the browser.",
                    },
                },
                "required": ["url"],
            },
            handler=browser_goto,
            # Opening a URL is navigation/read, not an external write. Keeping
            # this non-blocking prevents a missing approval UI from making the
            # model claim that browser automation is unavailable.
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.BROWSER,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        ToolDef(
            name="browser_read",
            description="Read the current browser page and return actionable elements with refs.",
            input_schema={
                "type": "object",
                "properties": {
                    "visibility": {"type": "string", "enum": ["auto", "background", "foreground", "silent", "visible"], "default": "auto"},
                    "goal": {"type": "string", "default": ""},
                },
            },
            handler=browser_read,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.BROWSER,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
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
                    "visibility": {"type": "string", "enum": ["auto", "background", "foreground", "silent", "visible"], "default": "auto"},
                },
            },
            handler=browser_act,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.BROWSER,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.EXTERNAL_WRITE,
            runtime_policy=_browser_act_runtime_policy,
        ),
        ToolDef(
            name="browser_extract",
            description="Extract readable text from the current browser page.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "visibility": {"type": "string", "enum": ["auto", "background", "foreground", "silent", "visible"], "default": "auto"},
                },
            },
            handler=browser_extract,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.BROWSER,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
    ]
    registry.register_bulk({tool.name: tool for tool in tools})
