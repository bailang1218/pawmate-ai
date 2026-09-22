"""MCP tool loader.

Loads local MCP tools into the registry. Failures fall back to the built-in
tools so startup remains usable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import pawmate.config as config
from pawmate.tools.core.registry import ToolRegistry
from pawmate.tools.mcp.client import load_mcp_server

_logger = logging.getLogger("pawmate")


async def load_mcp_tools(registry: ToolRegistry) -> Optional[int]:
    """Load tools from the bundled MCP server into the registry."""
    enable_mcp = os.getenv("PAWMATE_ENABLE_MCP", "0") == "1"
    if not enable_mcp:
        _logger.info("[MCP] disabled by default; set PAWMATE_ENABLE_MCP=1 to enable")
        return None

    server_path = config.BASE_DIR / "tools" / "mcp" / "server.py"
    if not server_path.exists():
        _logger.info("[MCP] server not found: %s", server_path)
        return None

    started = time.perf_counter()
    try:
        raw_timeout = os.getenv(
            "PAWMATE_MCP_SERVER_TIMEOUT",
            str(getattr(config, "MCP_SERVER_TIMEOUT", 8)),
        )
        timeout_s = max(3, int(raw_timeout))
        _logger.info("[MCP] loading tools from %s timeout=%ss", server_path.name, timeout_s)
        count = await asyncio.wait_for(
            load_mcp_server(registry, str(server_path)),
            timeout=timeout_s,
        )
        elapsed = time.perf_counter() - started
        _logger.info("[MCP] loaded %d tools in %.2fs", count, elapsed)
        return count
    except Exception as exc:
        elapsed = time.perf_counter() - started
        _logger.warning(
            "[MCP] tool loading failed after %.2fs; falling back to built-ins: %s",
            elapsed,
            exc,
        )
        return None
