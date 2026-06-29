from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict

from pawmate.core.tool_result_budget import ToolResultViews, budget_tool_result
from pawmate.tools.registry import ToolRegistry


logger = logging.getLogger("pawmate")


@dataclass(frozen=True)
class ToolCallOutcome:
    success: bool
    views: ToolResultViews
    elapsed: float
    error: Exception | None = None


class ToolCallRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._registry = registry
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    async def run(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        *,
        session_id: str = "",
    ) -> ToolCallOutcome:
        start_t = time.time()
        try:
            result = await self._execute_tool_with_retry(tool_name, tool_input)
            elapsed = time.time() - start_t
            views = budget_tool_result(tool_name, result, session_id=session_id)
            return ToolCallOutcome(success=True, views=views, elapsed=elapsed)
        except Exception as exc:
            elapsed = time.time() - start_t
            views = budget_tool_result(
                tool_name,
                f"[tool_error] {exc}",
                session_id=session_id,
            )
            return ToolCallOutcome(success=False, views=views, elapsed=elapsed, error=exc)

    async def _execute_tool_with_retry(self, tool_name: str, tool_input: Dict[str, Any]):
        last_exception = None

        for attempt in range(self._max_retries + 1):
            try:
                if attempt > 0:
                    await asyncio.sleep(self._retry_delay * (2 ** (attempt - 1)))

                return await self._registry.execute(tool_name, tool_input)

            except asyncio.TimeoutError as exc:
                last_exception = exc
                logger.warning(
                    "[Engine] %s timed out (attempt %s/%s)",
                    tool_name,
                    attempt + 1,
                    self._max_retries + 1,
                )
                continue

            except ConnectionError as exc:
                last_exception = exc
                logger.warning(
                    "[Engine] %s connection error (attempt %s/%s): %s",
                    tool_name,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                continue

            except Exception as exc:
                logger.warning("[Engine] %s failed without retry: %s", tool_name, exc)
                raise

        raise last_exception
