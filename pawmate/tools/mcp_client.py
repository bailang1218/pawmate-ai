"""Stable stdio JSON-RPC client for local MCP servers."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from pawmate.tools.registry import (
    APPROVAL_AUTO,
    APPROVAL_CONFIRM,
    APPROVAL_NOTIFY,
    ToolDef,
    ToolRegistry,
)


logger = logging.getLogger("pawmate")


class _POpenWrapper:
    """Adapter that gives subprocess.Popen the small async Process shape we use."""

    def __init__(self, popen: subprocess.Popen):
        self._popen = popen
        self.stdin = popen.stdin
        self.stdout = popen.stdout
        self.stderr = popen.stderr

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    async def wait(self) -> int:
        return await asyncio.to_thread(self._popen.wait)

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()


class MCPBridge:
    """Single MCP stdio JSON-RPC bridge."""

    def __init__(self, server_script: str):
        self._server_script = str(Path(server_script).resolve())
        self._process: Optional[Any] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._stderr_tail: list[str] = []
        self._initialized = False

    async def connect(self) -> None:
        if self._process is not None and self._process.returncode is None and self._initialized:
            return

        async with self._connect_lock:
            if self._process is not None and self._process.returncode is None and self._initialized:
                return

            await self.close()
            self._process = await self._create_process()

            try:
                self._reader_task = asyncio.create_task(self._read_stdout_loop())
                self._stderr_task = asyncio.create_task(self._read_stderr_loop())
                await self._initialize()
                self._initialized = True
            except Exception:
                await self.close()
                raise

    async def _create_process(self) -> Any:
        def create_popen() -> _POpenWrapper:
            popen = subprocess.Popen(
                [sys.executable, "-u", self._server_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return _POpenWrapper(popen)

        if sys.platform == "win32":
            return await asyncio.to_thread(create_popen)

        try:
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                self._server_script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except NotImplementedError:
            return await asyncio.to_thread(create_popen)

    async def close(self) -> None:
        self._initialized = False

        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("MCP connection closed"))
        self._pending.clear()

        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = None
        self._stderr_task = None

        process = self._process
        if process is not None:
            try:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()

                for stream_name in ("stdin", "stdout", "stderr"):
                    await self._close_stream(getattr(process, stream_name, None))
            except Exception:
                logger.debug("[MCP] process close failed", exc_info=True)
            finally:
                self._process = None

    async def _close_stream(self, stream: Any) -> None:
        if stream is None:
            return
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        wait_closed = getattr(stream, "wait_closed", None)
        if callable(wait_closed):
            try:
                result = wait_closed()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    async def list_tools(self) -> Dict[str, Any]:
        await self.connect()
        result = await self._request("tools/list", {})
        if not isinstance(result, dict):
            return {"tools": []}
        return result

    async def call_tool(self, tool_name: str, kwargs: Dict[str, Any]) -> str:
        await self.connect()
        result = await self._request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": kwargs,
            },
        )

        if not isinstance(result, dict):
            return str(result)

        content = result.get("content", [])
        texts = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    texts.append(str(item.get("text", "")))

        if texts:
            return "\n".join(texts)
        if "result" in result:
            return str(result["result"])
        return json.dumps(result, ensure_ascii=False)

    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "pawmate-mcp-bridge",
                    "version": "2.1.0",
                },
            },
            timeout=max(3, int(os.getenv("PAWMATE_MCP_CONNECT_TIMEOUT", "8"))),
        )
        await self._notify("notifications/initialized", {})

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _request(self, method: str, params: Dict[str, Any], timeout: int = 20) -> Any:
        if self._process is None or self._process.returncode is not None:
            raise RuntimeError("MCP process is not running")

        req_id = self._next_id
        self._next_id += 1

        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        try:
            await self._send_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": method,
                    "params": params,
                }
            )
            response = await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            self._pending.pop(req_id, None)
            raise

        if isinstance(response, dict) and "error" in response:
            raise RuntimeError(f"MCP RPC error: {response['error']}")
        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    async def _send_message(self, message: Dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP stdin is unavailable")

        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")

        async with self._write_lock:
            stdin = self._process.stdin
            if hasattr(stdin, "drain"):
                stdin.write(payload)
                await stdin.drain()
            else:
                await asyncio.to_thread(stdin.write, payload)
                await asyncio.to_thread(stdin.flush)

    async def _read_stdout_loop(self) -> None:
        if self._process is None or self._process.stdout is None:
            return

        reader = self._process.stdout
        try:
            while True:
                line = await self._readline(reader)
                if not line:
                    raise EOFError("MCP stdout closed")

                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                try:
                    message = json.loads(text)
                except Exception:
                    logger.debug("[MCP] ignored non-JSON stdout line: %s", text[:200])
                    continue

                msg_id = message.get("id")
                if isinstance(msg_id, int) and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(message)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            err = RuntimeError(f"MCP response read failed: {exc}. stderr: {self._stderr_snapshot()}")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _read_stderr_loop(self) -> None:
        if self._process is None or self._process.stderr is None:
            return

        reader = self._process.stderr
        try:
            while True:
                line = await self._readline(reader)
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > 40:
                        self._stderr_tail = self._stderr_tail[-40:]
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("[MCP] stderr read loop failed", exc_info=True)

    async def _readline(self, reader: Any) -> bytes:
        if isinstance(reader, asyncio.StreamReader):
            return await reader.readline()
        readline = getattr(reader, "readline")
        if asyncio.iscoroutinefunction(readline):
            return await readline()
        return await asyncio.to_thread(readline)

    def _stderr_snapshot(self) -> str:
        if not self._stderr_tail:
            return "(empty)"
        return " | ".join(self._stderr_tail[-5:])


async def load_mcp_server(registry: ToolRegistry, server_script: str) -> int:
    """Load one MCP server script and register its tools."""
    script_path = Path(server_script)
    if not script_path.exists():
        raise FileNotFoundError(f"MCP server script does not exist: {server_script}")

    bridge = MCPBridge(str(script_path))

    try:
        tools_resp = await bridge.list_tools()
    except Exception as exc:
        await bridge.close()
        import traceback

        detailed_error = traceback.format_exc()
        error_msg = (
            f"MCP connect/handshake failed ({type(exc).__name__}: {exc}). "
            f"stderr tail: {bridge._stderr_snapshot()}"
        )
        logger.error("[MCP ERROR] %s\n%s", error_msg, detailed_error)
        raise RuntimeError(error_msg) from exc

    tools = tools_resp.get("tools", []) if isinstance(tools_resp, dict) else []
    count = 0
    for tool in tools:
        _register_tool(registry, bridge, tool)
        count += 1

    registry.add_cleanup_hook(bridge.close)
    logger.info("[MCP] loaded %s tools from %s", count, script_path.name)

    await bridge.close()
    return count


_MCP_HIGH_RISK_KEYWORDS = [
    "run",
    "shell",
    "command",
    "exec",
    "script",
    "write",
    "patch",
    "replace",
    "delete",
    "remove",
    "move",
    "rename",
    "kill",
    "process",
    "launch",
    "install",
    "uninstall",
    "upload",
]


def _mcp_approval_level(mcp_tool_name: str, mcp_tool: dict) -> str:
    name_lower = mcp_tool_name.lower()
    desc_lower = (mcp_tool.get("description", "") or "").lower()
    combined = f"{name_lower} {desc_lower}"

    for keyword in _MCP_HIGH_RISK_KEYWORDS:
        if keyword in combined:
            return APPROVAL_CONFIRM

    if "read" in combined:
        return APPROVAL_CONFIRM

    for keyword in ("list", "get", "search", "status", "inspect"):
        if keyword in combined:
            return APPROVAL_AUTO

    if "open" in combined:
        return APPROVAL_NOTIFY

    return APPROVAL_CONFIRM


def _register_tool(
    registry: ToolRegistry,
    bridge: MCPBridge,
    mcp_tool: Any,
    server_name: str = "mcp",
) -> None:
    if not isinstance(mcp_tool, dict):
        return

    tool_name = str(mcp_tool.get("name", "")).strip()
    if not tool_name:
        return

    description = str(mcp_tool.get("description", "") or "")
    input_schema = (
        mcp_tool.get("inputSchema")
        or mcp_tool.get("input_schema")
        or {"type": "object", "properties": {}}
    )
    approval = _mcp_approval_level(tool_name, mcp_tool)
    source = f"mcp:{server_name}"

    async def handler(**kwargs) -> str:
        try:
            return await bridge.call_tool(tool_name, kwargs)
        except Exception as exc:
            return f"[error] {exc}"

    registry.register(
        ToolDef(
            name=tool_name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            approval=approval,
            source=source,
        )
    )
