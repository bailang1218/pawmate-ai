"""Deterministic router behind the browser facade tools."""

from __future__ import annotations

from dataclasses import dataclass
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
    launch_managed: Callable[[], Awaitable[None]]
    navigate: Callable[[str], Awaitable[None]]
    observe: Callable[[], Awaitable[dict]]
    click: Callable[[str], Awaitable[dict]]
    type_text: Callable[[str, str], Awaitable[dict]]
    fill: Callable[[str, str], Awaitable[dict]]
    extract: Callable[[str], Awaitable[dict]]
    act: Optional[Callable[[dict], Awaitable[dict]]] = None


@dataclass
class GotoResult:
    ok: bool
    url: str
    surface: str
    reachability: Optional[dict] = None
    note: str = ""


@dataclass
class ReadResult:
    ok: bool
    url: str
    title: str
    page_kind: str
    modality: str
    elements: list
    note: str = ""


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
        "href": item.get("href", ""),
    }


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

    @property
    def surface(self) -> BrowserSurface:
        return self._surface

    @property
    def events(self) -> list:
        return list(self._ladder.events)

    def set_goal(self, goal: str) -> None:
        self._goal = goal

    async def goto(self, url: str, *, use_my_login: bool = False, precheck: bool = True) -> GotoResult:
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
        else:
            self._surface = BrowserSurface(Target.MANAGED, Modality.DOM)
            await self._a.launch_managed()

        await self._a.navigate(url)

        reach = None
        if precheck:
            observed = await self._a.observe()
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
                )
        return GotoResult(ok=True, url=url, surface=self._surface.label(), reachability=reach)

    async def read(self) -> ReadResult:
        observed = await self._a.observe()
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
            )

        decision = self._ladder.on_dom_empty(current=self._surface)
        if self._vision is None:
            raise BrowserVisionUnavailable(
                "DOM 无可操作元素且视觉子 agent 未接入",
                from_surface=self._surface.label(),
                to_surface="vision",
                reason="dom_empty",
            )
        self._surface = BrowserSurface(decision.to_target, decision.to_modality)
        return ReadResult(
            ok=True,
            url=url,
            title=title,
            page_kind=page_kind,
            modality="vision",
            elements=[],
            note="DOM 为空，已切视觉；用 act(intent=...) 指定目标，不要用 ref。",
        )

    async def act(self, *, ref: str = "", action: str = "click", value: str = "", intent: str = "", **params) -> dict:
        if self._surface.modality == Modality.VISION:
            if self._vision is None:
                raise BrowserVisionUnavailable("视觉子 agent 未接入", reason="vision_unwired")
            target = intent or value or ref
            result: SubAgentResult = await self._vision.run(target=target, action=action)
            return {"ok": result.ok, "operation": "browser_act", "modality": "vision", **result.as_dict()}

        if self._a.act is not None:
            payload = {
                "ref": ref,
                "action": action,
                "value": value,
                "intent": intent,
                **params,
                "_require_native_session": self._surface.target == Target.NATIVE,
            }
            return await self._a.act(payload)

        if action == "click":
            return await self._a.click(ref)
        if action == "type":
            return await self._a.type_text(ref, value)
        if action == "fill":
            return await self._a.fill(ref, value)
        raise ValueError(f"unsupported action: {action}")

    async def extract(self, query: str = "") -> dict:
        return await self._a.extract(query)
