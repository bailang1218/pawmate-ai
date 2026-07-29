"""Adapter from workflow actions to PawMate's single browser facade."""

from __future__ import annotations

from typing import Any, Protocol


class BrowserRuntime(Protocol):
    async def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]: ...


class PawMateBrowserRuntime:
    """Use the existing BrowserRouter/Playwright session instead of a second runtime."""

    async def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        from pawmate.tools.browser.facade import browser_act, browser_extract, browser_goto, browser_read

        if action == "browser.goto":
            return await browser_goto(
                str(params.get("url") or ""),
                use_my_login=bool(params.get("use_my_login", False)),
                goal=str(params.get("goal") or ""),
                visibility=str(params.get("visibility") or "auto"),
            )
        if action == "browser.read":
            return await browser_read(
                visibility=str(params.get("visibility") or "auto"),
                goal=str(params.get("goal") or ""),
            )
        if action == "browser.extract":
            return await browser_extract(
                query=str(params.get("query") or ""),
                visibility=str(params.get("visibility") or "auto"),
            )
        if action in {
            "browser.click",
            "browser.fill",
            "browser.type",
            "browser.smart_type",
            "browser.key",
            "browser.hotkey",
            "browser.scroll",
        }:
            runtime_action = action.split(".", 1)[1]
            payload = {
                "ref": str(params.get("ref") or ""),
                "action": runtime_action,
                "value": str(params.get("value") or ""),
                "intent": str(params.get("intent") or ""),
                "key": str(params.get("key") or ""),
                "keys": list(params.get("keys") or []),
                "deltaX": int(params.get("deltaX", 0)),
                "deltaY": int(params.get("deltaY", 0)),
                "text": str(params.get("text") or ""),
                "clear": bool(params.get("clear", True)),
                "visibility": str(params.get("visibility") or "auto"),
            }
            return await browser_act(**payload)
        raise ValueError(f"Runtime cannot execute action: {action}")
