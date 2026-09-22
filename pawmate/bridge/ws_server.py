"""
PawMate WebSocket 服务器

让外部客户端通过 WebSocket 连接 PawMate。该入口默认关闭；
启用时应配置 auth_token 或 PAWMATE_WS_TOKEN。
端口：18765（config.WS_PORT）

客户端发：
  {"type": "chat", "text": "你好"}

认证方式：
  Authorization: Bearer <token>
  X-PawMate-Token: <token>
  ws://127.0.0.1:18765/?token=<token>

服务器推送：
  {"type": "text_delta", "text": "正在..."}
  {"type": "tool_start", "tool": "browser_search", "input": "..."}
  {"type": "tool_done", "tool": "browser_search", "result": "..."}
  {"type": "tool_error", "tool": "...", "error": "..."}
  {"type": "finished"}
  {"type": "error", "text": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import websockets

from pawmate.bridge.bridges.ws_bridge import (
    WsChatInbound,
    WsInvalidInbound,
    WsPingInbound,
    error_payload,
    parse_inbound,
    pong_payload,
    to_ws_payload,
)
from pawmate.bridge.contracts import AppEvent
from pawmate.bridge.event_bus import event_bus
from pawmate.bridge.worker import AgentWorker

logger = logging.getLogger("pawmate.ws")


class EventBridge:
    """
    桥接 Qt EventBus 信号 → asyncio Queue。
    WS 服务器的事件循环从 queue 中消费并推送给客户端。
    """

    def __init__(self):
        self._max_queue_size = 256
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._max_queue_size)
        self._client_queues: set[asyncio.Queue[dict]] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def connect(self):
        """连接 EventBus 信号（必须在 Qt 线程调用）"""
        if self._connected:
            return

        # Qt 信号 → 推入 asyncio queue
        event_bus.subscribe(AppEvent, self._on_event)
        self._connected = True

    def disconnect(self):
        """Disconnect from EventBus and stop feeding WebSocket clients."""
        if not self._connected:
            return
        event_bus.unsubscribe(AppEvent, self._on_event)
        self._connected = False
        loop = self._loop
        if loop and not loop.is_closed() and loop.is_running():
            loop.call_soon_threadsafe(self._client_queues.clear)
        else:
            self._client_queues.clear()

    def _push(self, data: dict):
        """把事件推给 WS 事件循环"""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._fan_out, data)

    def _fan_out(self, data: dict) -> None:
        self._put_drop_oldest(self._queue, data)
        for queue in tuple(self._client_queues):
            self._put_drop_oldest(queue, data)

    @staticmethod
    def _put_drop_oldest(queue: asyncio.Queue[dict], data: dict) -> None:
        try:
            queue.put_nowait(data)
            return
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def _on_event(self, event: AppEvent):
        payload = to_ws_payload(event)
        if payload is not None:
            self._push(payload)

    def create_reader(self) -> asyncio.Queue[dict]:
        """Create a per-client event queue so WebSocket clients receive broadcasts."""
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._max_queue_size)
        self._client_queues.add(queue)
        return queue

    def release_reader(self, queue: asyncio.Queue[dict]) -> None:
        self._client_queues.discard(queue)

    async def read(self) -> dict:
        """读取下一个事件（供 WS 协程消费）"""
        return await self._queue.get()


class WsChatServer:
    """WebSocket 聊天服务器"""

    def __init__(
        self,
        worker: AgentWorker,
        host: str = "127.0.0.1",
        port: int = 18765,
        auth_token: str = "",
    ):
        self._worker = worker
        self._host = host
        self._port = port
        self._auth_token = str(auth_token or "").strip()
        if not self._auth_token:
            raise ValueError("WebSocket auth_token is required")
        self._bridge = EventBridge()
        self._server: Optional[websockets.WebSocketServer] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self):
        """在新线程中启动 WS 服务器（非阻塞）"""
        # 在主线程连接 EventBus(由调用方负责)
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-server")
        self._thread.start()
        logger.info(f"WS 服务器线程已启动")

    def connect_bridge(self):
        """在 Qt 主线程调用：连接 EventBus 信号到 bridge"""
        self._bridge.connect()

    def _run(self):
        """线程主函数：创建自己的 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._bridge.set_loop(self._loop)

        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            logger.error(f"WS 服务器异常: {e}")
        finally:
            self._bridge.disconnect()
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._loop = None

    async def _serve(self):
        """启动 WebSocket 服务"""
        logger.info(f"WS 服务器启动 ws://{self._host}:{self._port}")
        self._server = await websockets.serve(
            self._handle_client,
            self._host,
            self._port,
            ping_interval=30,
            ping_timeout=10,
            max_size=64 * 1024,
            max_queue=16,
        )
        logger.info(f"WS 服务器已就绪")
        try:
            await self._server.wait_closed()
        except asyncio.CancelledError:
            pass

    async def _handle_client(self, websocket):
        """处理单个 WS 客户端连接"""
        # 为这个客户端创建独立的事件读取任务
        remote = websocket.remote_address
        if not await self._authorize_client(websocket):
            logger.warning("WS unauthorized client rejected: %s", remote)
            return
        logger.info(f"WS 客户端连接: {remote}")

        # 创建协程：从 bridge 读事件并推给 WS 客户端
        event_queue = self._bridge.create_reader()

        async def _push_events():
            try:
                while True:
                    event = await event_queue.get()
                    try:
                        await websocket.send(json.dumps(event, ensure_ascii=False))
                    except websockets.ConnectionClosed:
                        break
            except asyncio.CancelledError:
                pass

        push_task = asyncio.create_task(_push_events())

        try:
            async for raw in websocket:
                inbound = parse_inbound(raw)
                if isinstance(inbound, WsChatInbound):
                    text = inbound.text
                    if text:
                        logger.info(f"WS 收到消息: {text[:50]}")
                        await self._dispatch_chat(websocket, text)
                    else:
                        await websocket.send(json.dumps({
                            "type": "error", "text": "消息不能为空"
                        }))
                elif isinstance(inbound, WsPingInbound):
                    await websocket.send(json.dumps(pong_payload()))
                elif isinstance(inbound, WsInvalidInbound):
                    await websocket.send(json.dumps({
                        "type": "error", "text": inbound.error_text
                    }))

        except websockets.ConnectionClosed:
            logger.info(f"WS 客户端断开: {remote}")
        except Exception as e:
            if "Event loop is closed" not in str(e):
                logger.error(f"WS 处理异常: {e}")
        finally:
            push_task.cancel()
            try:
                await push_task
            except (asyncio.CancelledError, RuntimeError):
                pass
            self._bridge.release_reader(event_queue)

    async def _dispatch_chat(self, websocket, text: str) -> None:
        is_ready = getattr(self._worker, "is_ready", None)
        if callable(is_ready):
            try:
                if not is_ready():
                    await websocket.send(json.dumps(error_payload("backend worker not ready")))
                    return
            except Exception:
                logger.exception("WS worker readiness check failed")
                await websocket.send(json.dumps(error_payload("backend worker readiness check failed")))
                return
        try:
            self._worker.send_message(text)
        except Exception:
            logger.exception("WS worker dispatch failed")
            await websocket.send(json.dumps(error_payload("backend worker dispatch failed")))

    async def _authorize_client(self, websocket) -> bool:
        """Validate the optional bearer token before any events are streamed."""
        provided = self._extract_token(websocket)
        if provided and secrets.compare_digest(provided, self._auth_token):
            return True

        try:
            await websocket.send(json.dumps({
                "type": "error",
                "text": "unauthorized websocket client",
            }))
        except Exception:
            pass

        close = getattr(websocket, "close", None)
        if callable(close):
            try:
                result = close(code=1008, reason="unauthorized")
            except TypeError:
                result = close()
            except Exception:
                result = None
            if asyncio.iscoroutine(result):
                try:
                    await result
                except Exception:
                    pass
        return False

    def _extract_token(self, websocket) -> str:
        headers = getattr(websocket, "request_headers", {}) or {}

        def _header(name: str) -> str:
            try:
                return str(headers.get(name, "") or headers.get(name.lower(), "")).strip()
            except AttributeError:
                return ""

        auth_header = _header("Authorization")
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()

        header_token = _header("X-PawMate-Token")
        if header_token:
            return header_token

        return ""

    def stop(self):
        """停止 WS 服务器"""
        self._bridge.disconnect()
        loop = self._loop
        if loop and not loop.is_closed():
            if self._server is not None and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(self._close_server(), loop)
                try:
                    fut.result(timeout=5)
                except Exception:
                    logger.exception("WS server graceful shutdown timed out")
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(loop.stop)
            elif loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        # Python 3.14 ProactorEventLoop cleanup noise - harmless

    async def _close_server(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()


# 方便从外部启动
def create_ws_server(
    worker: AgentWorker,
    port: int = 18765,
    auth_token: str = "",
) -> WsChatServer:
    """创建并启动 WebSocket 服务器（快捷函数）"""
    server = WsChatServer(worker, port=port, auth_token=auth_token)
    server.start()
    return server
