"""
MCP 工具加载器

职责：从 MCP server 加载工具并注册到 ToolRegistry，失败时静默回退。
"""
import asyncio
import logging
import os
from typing import Optional

import pawmate.config as config
from pawmate.tools.registry import ToolRegistry
from pawmate.tools.mcp_client import load_mcp_server

_logger = logging.getLogger("pawmate")


async def load_mcp_tools(registry: ToolRegistry) -> Optional[int]:
    """
    从 MCP server 加载工具到注册表。

    MCP 可通过环境变量 PAWMATE_ENABLE_MCP=0 禁用。
    加载失败时静默回退，不影响引擎启动。

    Returns:
        加载的工具数量，或 None（禁用/失败/不可用）
    """
    enable_mcp = os.getenv("PAWMATE_ENABLE_MCP", "1") == "1"
    if not enable_mcp:
        return None

    server_path = config.BASE_DIR / "tools" / "mcp_server.py"
    if not server_path.exists():
        return None

    try:
        timeout_s = max(5, int(getattr(config, "MCP_SERVER_TIMEOUT", 30)))
        count = await asyncio.wait_for(
            load_mcp_server(registry, str(server_path)),
            timeout=timeout_s,
        )
        _logger.info("[MCP] 成功加载 %d 个 MCP 工具", count)
        return count
    except Exception as e:
        _logger.warning("[MCP] 工具加载失败，已回退到内置工具: %s", e)
        return None
