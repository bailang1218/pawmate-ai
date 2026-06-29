"""Typed browser escalation ladder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pawmate.core.browser.surface import BrowserSurface, Modality, Target


class BrowserEscalation(RuntimeError):
    """Base class for typed browser surface switches or hard stops."""

    def __init__(
        self,
        message: str,
        *,
        from_surface: str = "",
        to_surface: str = "",
        reason: str = "",
        evidence: str = "",
    ) -> None:
        super().__init__(message)
        self.from_surface = from_surface
        self.to_surface = to_surface
        self.reason = reason
        self.evidence = evidence


class CdpAttachFailed(BrowserEscalation):
    """The native browser could not be attached through CDP."""


class DomEmpty(BrowserEscalation):
    """DOM observation found no actionable elements."""


class VisionUnlocated(BrowserEscalation):
    """Vision could not locate the requested target."""


class LoginRequired(BrowserEscalation):
    """A login-state task cannot continue because the native profile is unavailable."""


@dataclass
class EscalationEvent:
    from_surface: str
    to_surface: str
    reason: str
    evidence: str = ""

    def as_dict(self) -> dict:
        return {
            "from": self.from_surface,
            "to": self.to_surface,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass
class LadderDecision:
    action: str
    to_target: Optional[Target] = None
    to_modality: Optional[Modality] = None
    reason: str = ""
    event: Optional[EscalationEvent] = None


@dataclass
class EscalationLadder:
    """Fixed-priority escalation with a bounded retry budget."""

    vision_retry_budget: int = 1
    _vision_attempts: int = field(default=0, init=False)
    events: list = field(default_factory=list, init=False)

    def _emit(self, event: EscalationEvent) -> EscalationEvent:
        self.events.append(event)
        return event

    def on_cdp_attach_failed(self, *, use_my_login: bool, current: BrowserSurface) -> LadderDecision:
        if use_my_login:
            event = self._emit(EscalationEvent(current.label(), "(halt)", "login_required"))
            return LadderDecision(
                action="ask_user",
                reason="需要登录态但无法挂载原生 profile，请用户确认",
                event=event,
            )
        event = self._emit(EscalationEvent(current.label(), "managed/dom", "cdp_attach_failed"))
        return LadderDecision(
            action="switch",
            to_target=Target.MANAGED,
            to_modality=Modality.DOM,
            reason="无登录态要求，降级 Playwright 托管浏览器",
            event=event,
        )

    def on_dom_empty(self, *, current: BrowserSurface) -> LadderDecision:
        target = current.with_(modality=Modality.VISION)
        event = self._emit(EscalationEvent(current.label(), target.label(), "dom_empty"))
        return LadderDecision(
            action="vision",
            to_target=current.target,
            to_modality=Modality.VISION,
            reason="DOM 无可操作元素，同目标升级视觉",
            event=event,
        )

    def on_vision_unlocated(self, *, current: BrowserSurface) -> LadderDecision:
        self._vision_attempts += 1
        if self._vision_attempts <= self.vision_retry_budget:
            event = self._emit(EscalationEvent("vision", "vision", f"locate_retry_{self._vision_attempts}"))
            return LadderDecision(action="retry", reason="重截图重试定位", event=event)
        event = self._emit(EscalationEvent(current.label(), "(halt)", "vision_unlocated"))
        return LadderDecision(action="stop", reason="多次无法定位目标，停止并上报", event=event)

