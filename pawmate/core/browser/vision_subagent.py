"""Bounded vision sub-agent for browser perceive-act loops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from pawmate.ocr_vision.models import LocateResult


ScreenshotFn = Callable[[], Awaitable[tuple]]
LocateFn = Callable[[str, str, tuple[int, int]], Awaitable[LocateResult]]
ClickFn = Callable[[int, int], Awaitable[None]]


@dataclass
class SubAgentResult:
    ok: bool
    action_taken: str
    steps: int
    final_state: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action_taken": self.action_taken,
            "steps": self.steps,
            "final_state": self.final_state,
            "reason": self.reason,
        }


class VisionSubAgent:
    def __init__(
        self,
        *,
        screenshot_fn: ScreenshotFn,
        locate_fn: LocateFn,
        click_fn: ClickFn,
        max_steps: int = 2,
        verify: bool = True,
    ) -> None:
        self._screenshot = screenshot_fn
        self._locate = locate_fn
        self._click = click_fn
        self.max_steps = max_steps
        self.verify = verify

    async def run(self, *, target: str, action: str = "click") -> SubAgentResult:
        last_reason = ""
        for step in range(1, self.max_steps + 1):
            data_url, size = await self._screenshot()
            loc = await self._locate(data_url, target, size)
            if not loc.ok:
                last_reason = loc.reason
                if step < self.max_steps:
                    continue
                return SubAgentResult(False, "", step, "unlocated", reason=last_reason)

            await self._click(loc.point["x"], loc.point["y"])
            acted = f"{action}@({loc.point['x']},{loc.point['y']})"
            if not self.verify:
                return SubAgentResult(True, acted, step, "acted")

            data_url2, size2 = await self._screenshot()
            loc2 = await self._locate(data_url2, target, size2)
            if not loc2.ok:
                return SubAgentResult(True, acted, step, "verified")
            # A button remaining visible does not prove that the click failed.
            # Never click the same visual guess repeatedly: report the dispatched
            # action as unverified and let the caller re-read DOM/page state.
            return SubAgentResult(True, acted, step, "acted_unverified", reason="target_still_present_after_action")

        return SubAgentResult(False, "", self.max_steps, "unlocated", reason=last_reason or "max_steps")
