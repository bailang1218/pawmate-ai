"""Deterministic router behind the browser facade tools."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Awaitable, Callable, Optional

from pawmate.core.browser.escalation import (
    BrowserEscalation,
    CdpAttachFailed,
    EscalationLadder,
    LoginRequired,
)
from pawmate.core.browser.page_kind import Reachability, assess_reachability, classify_page_kind
from pawmate.core.browser.surface import BrowserSurface, Modality, Target
from pawmate.core.browser.vision_subagent import SubAgentResult, VisionSubAgent


class BrowserVisionUnavailable(BrowserEscalation):
    """DOM is unavailable and the vision sub-agent is not wired."""


@dataclass
class BrowserAdapters:
    attach_native: Callable[[], Awaitable[None]]
    # Non-login browser session. The historical name is kept for compatibility,
    # but implementations should choose CDP-dedicated / headless / managed by
    # policy instead of forcing Playwright-managed foreground sessions.
    launch_managed: Callable[[str, str, str], Awaitable[str]]
    navigate: Callable[[str, str, str, bool], Awaitable[None]]
    observe: Callable[[str, str, bool], Awaitable[dict]]
    click: Callable[[str], Awaitable[dict]]
    type_text: Callable[[str, str], Awaitable[dict]]
    fill: Callable[[str, str], Awaitable[dict]]
    extract: Callable[[str, str, str, bool], Awaitable[dict]]
    act: Optional[Callable[[dict], Awaitable[dict]]] = None


@dataclass
class GotoResult:
    ok: bool
    url: str
    surface: str
    reachability: Optional[dict] = None
    note: str = ""
    visibility: str = "auto"
    require_native: bool = False


@dataclass
class ReadResult:
    ok: bool
    url: str
    title: str
    page_kind: str
    modality: str
    elements: list
    note: str = ""
    visibility: str = "auto"
    require_native: bool = False


def _actionable(items: list) -> bool:
    for item in items:
        if item.get("disabled"):
            continue
        if item.get("text") or item.get("href"):
            return True
    return False


def _normalize(item: dict) -> dict:
    return {
        "ref": item.get("selector", ""),
        "role": item.get("role") or item.get("tag", ""),
        "text": item.get("text", ""),
        "name": item.get("name", ""),
        "aria_label": item.get("ariaLabel", ""),
        "href": item.get("href", ""),
        "bbox": item.get("bbox"),
    }


def _is_auto_visibility(value: str) -> bool:
    return (value or "auto").strip().lower() in {"", "auto"}


def _recover_ref(items: list, intent: str) -> str:
    """Recover a stale selector only when fresh DOM has one clear label match."""
    needle = re.sub(r"\s+", "", (intent or "").casefold())
    if not needle:
        return ""
    matches: list[str] = []
    for item in items:
        selector = str(item.get("selector") or "")
        if not selector or item.get("disabled"):
            continue
        label = " ".join(str(item.get(key) or "") for key in ("text", "name", "ariaLabel", "title"))
        haystack = re.sub(r"\s+", "", label.casefold())
        if needle in haystack or (len(haystack) >= 2 and haystack in needle):
            matches.append(selector)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else ""


class BrowserRouter:
    def __init__(
        self,
        adapters: BrowserAdapters,
        *,
        vision_subagent: Optional[VisionSubAgent] = None,
        ladder: Optional[EscalationLadder] = None,
        current_goal: str = "",
    ) -> None:
        self._a = adapters
        self._vision = vision_subagent
        self._ladder = ladder or EscalationLadder()
        self._surface = BrowserSurface(Target.MANAGED, Modality.DOM)
        self._goal = current_goal
        # The session route is task state, not prompt state. Keep it even when
        # get_router(goal="") clears stale goal text, so browser_read(auto) after
        # browser_goto(background/native) continues to read the same session slot.
        self._last_visibility = "foreground"
        self._last_require_native = False

    @property
    def surface(self) -> BrowserSurface:
        return self._surface

    @property
    def events(self) -> list:
        return list(self._ladder.events)

    def set_goal(self, goal: str) -> None:
        self._goal = goal

    def _effective_visibility(self, visibility: str) -> str:
        if _is_auto_visibility(visibility):
            return self._last_visibility or "foreground"
        return visibility

    def _remember_route(self, visibility: str, *, require_native: bool) -> None:
        self._last_visibility = visibility or "foreground"
        self._last_require_native = bool(require_native)

    async def goto(
        self,
        url: str,
        *,
        use_my_login: bool = False,
        precheck: bool = True,
        visibility: str = "auto",
    ) -> GotoResult:
        require_native = bool(use_my_login)
        if use_my_login:
            self._surface = BrowserSurface(Target.NATIVE, Modality.DOM)
            try:
                await self._a.attach_native()
            except CdpAttachFailed as exc:
                decision = self._ladder.on_cdp_attach_failed(use_my_login=True, current=self._surface)
                error = LoginRequired(
                    decision.reason,
                    from_surface=self._surface.label(),
                    to_surface="(halt)",
                    reason="login_required",
                    evidence=str(exc),
                )
                setattr(error, "diagnostics", getattr(exc, "diagnostics", {}))
                raise error from exc
            resolved_visibility = "foreground"
        else:
            self._surface = BrowserSurface(Target.MANAGED, Modality.DOM)
            resolved_visibility = await self._a.launch_managed(visibility, self._goal, url)
            resolved_visibility = resolved_visibility or "foreground"

        self._remember_route(resolved_visibility, require_native=require_native)
        await self._a.navigate(url, resolved_visibility, self._goal, require_native)

        reach = None
        if precheck:
            observed = await self._a.observe(resolved_visibility, self._goal, require_native)
            reachability: Reachability = assess_reachability(
                self._goal,
                observed.get("url", url),
                observed.get("items", []),
            )
            reach = {
                "reachable": reachability.reachable,
                "page_kind": reachability.page_kind,
                "reason": reachability.reason,
                "suggestion": reachability.suggestion,
            }
            if not reachability.reachable:
                return GotoResult(
                    ok=False,
                    url=url,
                    surface=self._surface.label(),
                    reachability=reach,
                    note=reachability.suggestion or reachability.reason,
                    visibility=resolved_visibility,
                    require_native=require_native,
                )
        return GotoResult(
            ok=True,
            url=url,
            surface=self._surface.label(),
            reachability=reach,
            visibility=resolved_visibility,
            require_native=require_native,
        )

    async def read(self, visibility: str = "auto") -> ReadResult:
        resolved_visibility = self._effective_visibility(visibility)
        require_native = self._last_require_native or self._surface.target == Target.NATIVE
        observed = await self._a.observe(resolved_visibility, self._goal, require_native)
        url = observed.get("url", "")
        title = observed.get("title", "")
        items = observed.get("items", [])
        page_kind = classify_page_kind(url, items)

        if _actionable(items):
            return ReadResult(
                ok=True,
                url=url,
                title=title,
                page_kind=page_kind,
                modality="dom",
                elements=[_normalize(item) for item in items],
                visibility=resolved_visibility,
                require_native=require_native,
            )

        self._ladder.on_dom_empty(current=self._surface)
        if self._vision is None:
            raise BrowserVisionUnavailable(
                "DOM 无可操作元素且视觉子 agent 未接入",
                from_surface=self._surface.label(),
                to_surface="vision",
                reason="dom_empty",
            )
        return ReadResult(
            ok=True,
            url=url,
            title=title,
            page_kind=page_kind,
            modality="vision",
            elements=[],
            note="DOM 为空，已切视觉；用 act(intent=...) 指定目标，不要用 ref。",
            visibility=resolved_visibility,
            require_native=require_native,
        )

    async def act(self, *, ref: str = "", action: str = "click", value: str = "", intent: str = "", **params) -> dict:
        if self._surface.modality == Modality.VISION:
            if self._vision is None:
                raise BrowserVisionUnavailable("视觉子 agent 未接入", reason="vision_unwired")
            target = intent or value or ref
            result: SubAgentResult = await self._vision.run(target=target, action=action)
            return {"ok": result.ok, "operation": "browser_act", "modality": "vision", **result.as_dict()}

        visibility = self._effective_visibility(str(params.get("visibility") or "auto"))
        require_native = self._last_require_native or self._surface.target == Target.NATIVE
        if action == "click" and not ref and intent:
            observed = await self._a.observe(visibility, self._goal, require_native)
            ref = _recover_ref(observed.get("items", []), intent)
        if action == "click" and not ref and intent and self._vision is not None:
            result = await self._vision.run(target=intent, action=action)
            return {
                "ok": result.ok,
                "operation": "browser_act",
                "modality": "vision",
                "fallback": "missing_dom_ref",
                **result.as_dict(),
            }
        if self._a.act is not None:
            payload = {
                "ref": ref,
                "action": action,
                "value": value,
                "intent": intent,
                **params,
                "_require_native_session": require_native,
                "_visibility": visibility,
            }
            result = await self._a.act(payload)
            retryable = isinstance(result, dict) and not result.get("ok") and result.get("error_type") in {
                "bad_args",
                "all_backends_failed",
                "selector_not_found",
            }
            if retryable and action == "click" and intent and self._vision is not None:
                observed = await self._a.observe(visibility, self._goal, require_native)
                recovered_ref = _recover_ref(observed.get("items", []), intent)
                if recovered_ref and recovered_ref != ref:
                    recovered = await self._a.act({**payload, "ref": recovered_ref})
                    if not isinstance(recovered, dict) or recovered.get("ok"):
                        if isinstance(recovered, dict):
                            recovered.setdefault("fallback", "refreshed_dom_ref")
                            recovered.setdefault("previous_dom_error", result)
                        return recovered
                visual = await self._vision.run(target=intent, action=action)
                return {
                    "ok": visual.ok,
                    "operation": "browser_act",
                    "modality": "vision",
                    "fallback": "dom_action_failed",
                    "dom_error": result,
                    **visual.as_dict(),
                }
            return result

        if require_native:
            raise LoginRequired(
                "native session is required but the generic browser action adapter is not available",
                from_surface=self._surface.label(),
                to_surface="(halt)",
                reason="native_action_adapter_missing",
            )
        if action == "click":
            return await self._a.click(ref)
        if action == "type":
            return await self._a.type_text(ref, value)
        if action == "fill":
            return await self._a.fill(ref, value)
        raise ValueError(f"unsupported action: {action}")

    async def extract(self, query: str = "", visibility: str = "auto") -> dict:
        resolved_visibility = self._effective_visibility(visibility)
        require_native = self._last_require_native or self._surface.target == Target.NATIVE
        return await self._a.extract(query, self._goal, resolved_visibility, require_native)
