"""Registration entry point for browser facade tools."""

from __future__ import annotations

from pawmate.tools.core.registry import ToolRegistry

from .facade import register_browser_facade_tools


def register_browser_tools(registry: ToolRegistry) -> None:
    register_browser_facade_tools(registry)

    async def _close_browser_runtime() -> None:
        from .playwright_runtime import browser_close

        await browser_close("all")

    registry.add_cleanup_hook(_close_browser_runtime)
