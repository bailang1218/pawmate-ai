"""Local WebSocket bridge for the user's logged-in ChatGPT page.

The bridge is intentionally separate from Playwright. A small browser extension
connects from chatgpt.com to this localhost server, and PawMate tools can then
send prompts into the already logged-in page and receive the rendered reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import websockets

from pawmate.tools.registry import APPROVAL_CONFIRM, ToolDef, ToolRegistry

logger = logging.getLogger("pawmate.chatgpt_bridge")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18766
DEFAULT_TIMEOUT = 120


class ChatGPTBridgeServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str = ""):
        self.host = host
        self.port = port
        self.token = token.strip()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._clients: set[Any] = set()
        self._client_meta: dict[Any, dict[str, Any]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._started = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._started.clear()
        self._thread = threading.Thread(target=self._run, name="chatgpt-bridge", daemon=True)
        self._thread.start()
        self._started.wait(timeout=3)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()
            self._loop = None

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handle_client, self.host, self.port, ping_interval=20, ping_timeout=10)
        self._started.set()
        logger.info("[ChatGPTBridge] listening on ws://%s:%s", self.host, self.port)
        await self._server.wait_closed()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._loop is not None

    async def _handle_client(self, websocket) -> None:
        if self.token:
            token = self._extract_token(websocket)
            if not token or not secrets.compare_digest(token, self.token):
                await websocket.close(code=1008, reason="unauthorized")
                return
        self._clients.add(websocket)
        self._client_meta[websocket] = {"connected_at": time.time(), "url": "", "title": ""}
        try:
            async for raw in websocket:
                await self._handle_message(websocket, raw)
        finally:
            self._clients.discard(websocket)
            self._client_meta.pop(websocket, None)

    def _extract_token(self, websocket) -> str:
        path = str(getattr(websocket, "path", "") or "")
        if "?" in path:
            query = path.split("?", 1)[1]
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key in {"token", "auth_token"}:
                    return value
        return ""

    async def _handle_message(self, websocket, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        msg_type = data.get("type")
        if msg_type in {"hello", "state"}:
            meta = self._client_meta.setdefault(websocket, {})
            meta.update({
                "url": data.get("url", meta.get("url", "")),
                "title": data.get("title", meta.get("title", "")),
                "last_seen": time.time(),
            })
            return
        if msg_type in {"response", "error"}:
            request_id = str(data.get("id") or "")
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(data)

    def status(self) -> dict[str, Any]:
        clients = []
        for ws, meta in self._client_meta.items():
            clients.append({
                "url": meta.get("url", ""),
                "title": meta.get("title", ""),
                "connected_at": meta.get("connected_at", 0),
                "last_seen": meta.get("last_seen", 0),
            })
        return {
            "ok": True,
            "operation": "chatgpt_bridge_status",
            "running": self.is_running(),
            "url": f"ws://{self.host}:{self.port}",
            "clients": clients,
            "client_count": len(clients),
            "extension_dir": str(_extension_dir()),
        }

    async def _send_prompt_async(self, prompt: str, timeout: int) -> dict[str, Any]:
        if not self._clients:
            return {
                "ok": False,
                "operation": "chatgpt_bridge_send",
                "error_type": "no_chatgpt_page",
                "message": "No ChatGPT page is connected. Load the PawMate ChatGPT Bridge extension and open https://chatgpt.com.",
                "extension_dir": str(_extension_dir()),
            }
        request_id = secrets.token_hex(8)
        assert self._loop is not None
        future = self._loop.create_future()
        self._pending[request_id] = future
        payload = {"type": "send_prompt", "id": request_id, "text": prompt, "timeout": timeout}
        # Prefer the most recently seen page.
        clients = sorted(
            self._clients,
            key=lambda ws: self._client_meta.get(ws, {}).get("last_seen", self._client_meta.get(ws, {}).get("connected_at", 0)),
            reverse=True,
        )
        try:
            await clients[0].send(json.dumps(payload, ensure_ascii=False))
            result = await asyncio.wait_for(future, timeout=max(5, min(timeout + 10, 600)))
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return {"ok": False, "operation": "chatgpt_bridge_send", "error_type": "timeout", "message": "Timed out waiting for ChatGPT page response."}
        except Exception as exc:
            self._pending.pop(request_id, None)
            return {"ok": False, "operation": "chatgpt_bridge_send", "error_type": "send_failed", "message": str(exc)}
        if result.get("type") == "error":
            return {"ok": False, "operation": "chatgpt_bridge_send", "error_type": "page_error", "message": result.get("message", "")}
        return {
            "ok": True,
            "operation": "chatgpt_bridge_send",
            "text": result.get("text", ""),
            "url": result.get("url", ""),
            "title": result.get("title", ""),
        }

    async def send_prompt(self, prompt: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        self.start()
        if self._loop is None:
            return {"ok": False, "operation": "chatgpt_bridge_send", "error_type": "server_unavailable", "message": "Bridge server is not running."}
        future = asyncio.run_coroutine_threadsafe(self._send_prompt_async(prompt, timeout), self._loop)
        return await asyncio.wrap_future(future)


_server: ChatGPTBridgeServer | None = None


def _extension_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tools" / "chatgpt_bridge_extension"


def _get_server() -> ChatGPTBridgeServer:
    global _server
    if _server is None:
        token = os.getenv("PAWMATE_CHATGPT_BRIDGE_TOKEN", "")
        _server = ChatGPTBridgeServer(token=token)
    return _server


async def chatgpt_bridge_status() -> dict[str, Any]:
    server = _get_server()
    server.start()
    return server.status()


async def chatgpt_bridge_send(prompt: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    if not prompt:
        return {"ok": False, "operation": "chatgpt_bridge_send", "message": "prompt is required"}
    return await _get_server().send_prompt(prompt, timeout=timeout)


def register_chatgpt_bridge_tools(registry: ToolRegistry) -> None:
    registry.register_bulk({
        "chatgpt_bridge_status": ToolDef(
            name="chatgpt_bridge_status",
            description="启动并检查 PawMate 到用户已登录 ChatGPT 网页的本地隧道。需要先在用户浏览器加载 tools/chatgpt_bridge_extension 扩展并打开 chatgpt.com；Chromium 系浏览器通常可直接加载该扩展。",
            input_schema={"type": "object", "properties": {}},
            handler=chatgpt_bridge_status,
        ),
        "chatgpt_bridge_send": ToolDef(
            name="chatgpt_bridge_send",
            description="通过本地隧道把问题发送到用户已登录的 ChatGPT 网页，并读取网页返回的回复。适合利用用户浏览器中的 ChatGPT 登录态。",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 10, "maximum": 600, "default": DEFAULT_TIMEOUT},
                },
                "required": ["prompt"],
            },
            handler=chatgpt_bridge_send,
            approval=APPROVAL_CONFIRM,
        ),
    })
