"""
WebSocket 聊天客户端 —— Qt 窗口通过它和引擎通信

连接本地的 WebSocket 服务器（18765），收发消息。
UI 不需要直接碰 EventBus 和 Worker。
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable, Optional

import websockets


class WsChatClient:
    """
    WebSocket 聊天客户端。

    用法：
      client = WsChatClient()
      client.on_text_delta = lambda text: print(text)
      client.start()  # 在新线程连接
      client.send("你好")
      client.stop()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18765,
        max_queue_messages: int = 64,
        max_queue_bytes: int = 512 * 1024,
        drop_strategy: str = "drop_oldest",
    ):
        self._host = host
        self._port = port
        self._url = f"ws://{host}:{port}"
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._send_queue: list[str] = []  # 未连接时排队
        self._send_queue_bytes = 0
        self._queue_lock = threading.Lock()
        self._max_queue_messages = max(1, int(max_queue_messages))
        self._max_queue_bytes = max(1024, int(max_queue_bytes))
        self._drop_strategy = drop_strategy if drop_strategy in {"drop_oldest", "drop_newest"} else "drop_oldest"
        self.on_text_delta: Optional[Callable[[str], None]] = None
        self.on_tool_start: Optional[Callable[[str, str], None]] = None
        self.on_tool_done: Optional[Callable[[str, str], None]] = None
        self.on_tool_error: Optional[Callable[[str, str], None]] = None
        self.on_finished: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None

    def start(self):
        """在新线程启动 WS 客户端"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-client")
        self._thread.start()

    def _run(self):
        """线程主函数"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        except Exception:
            pass
        finally:
            self._loop.close()

    async def _connect_loop(self):
        """不断重试连接直到成功"""
        while self._running:
            try:
                self._ws = await websockets.connect(
                    self._url,
                    ping_interval=10,
                    ping_timeout=5,
                    max_size=10 * 1024 * 1024,  # 10MB
                )
                self._on_connected()
                await self._read_loop()
            except (OSError, websockets.ConnectionClosed) as e:
                if self._running:
                    self._on_error(f"连接断开: {e}")
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self._on_error(f"连接异常: {e}")
                    await asyncio.sleep(2)

    async def _read_loop(self):
        """读取服务器推送的事件"""
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")
            if msg_type == "text_delta":
                self._safe_call(self.on_text_delta, data["text"])
            elif msg_type == "tool_start":
                self._safe_call(self.on_tool_start, data["tool"], data.get("input", ""))
            elif msg_type == "tool_done":
                self._safe_call(self.on_tool_done, data["tool"], data.get("result", ""))
            elif msg_type == "tool_error":
                self._safe_call(self.on_tool_error, data["tool"], data.get("error", ""))
            elif msg_type == "finished":
                self._safe_call(self.on_finished)
            elif msg_type == "error":
                self._safe_call(self.on_error, data.get("text", ""))

    def send(self, text: str):
        """Send a chat message; queue it with limits while disconnected."""
        if self._ws and self._loop and not self._loop.is_closed():
            for msg in self._drain_send_queue():
                p = json.dumps({"type": "chat", "text": msg}, ensure_ascii=False)
                asyncio.run_coroutine_threadsafe(self._ws.send(p), self._loop)
            payload = json.dumps({"type": "chat", "text": text}, ensure_ascii=False)
            asyncio.run_coroutine_threadsafe(self._ws.send(payload), self._loop)
        else:
            self._enqueue_send(text)

    def _payload_size(self, text: str) -> int:
        payload = json.dumps({"type": "chat", "text": text}, ensure_ascii=False)
        return len(payload.encode("utf-8", errors="replace"))

    def _enqueue_send(self, text: str) -> bool:
        size = self._payload_size(text)
        if size > self._max_queue_bytes:
            self._on_error("WS send queue rejected oversized message")
            return False

        dropped = 0
        with self._queue_lock:
            if self._drop_strategy == "drop_newest":
                if (
                    len(self._send_queue) >= self._max_queue_messages
                    or self._send_queue_bytes + size > self._max_queue_bytes
                ):
                    self._on_error("WS send queue full; dropped newest message")
                    return False
            else:
                while (
                    self._send_queue
                    and (
                        len(self._send_queue) >= self._max_queue_messages
                        or self._send_queue_bytes + size > self._max_queue_bytes
                    )
                ):
                    old = self._send_queue.pop(0)
                    self._send_queue_bytes = max(0, self._send_queue_bytes - self._payload_size(old))
                    dropped += 1

            self._send_queue.append(text)
            self._send_queue_bytes += size

        if dropped:
            self._on_error(f"WS send queue full; dropped {dropped} oldest message(s)")
        return True

    def _drain_send_queue(self) -> list[str]:
        with self._queue_lock:
            queued = list(self._send_queue)
            self._send_queue.clear()
            self._send_queue_bytes = 0
        return queued

    def stop(self):
        """停止客户端"""
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)
        self._on_disconnected()

    def _on_connected(self):
        for msg in self._drain_send_queue():
            p = json.dumps({"type": "chat", "text": msg}, ensure_ascii=False)
            asyncio.run_coroutine_threadsafe(self._ws.send(p), self._loop)
        self._safe_call(self.on_connected)

    def _on_disconnected(self):
        self._safe_call(self.on_disconnected)

    def _on_error(self, msg: str):
        self._safe_call(self.on_error, msg)

    def _safe_call(self, callback, *args):
        """安全调用回调（可能在任意线程触发）"""
        if callback:
            try:
                callback(*args)
            except Exception:
                pass
