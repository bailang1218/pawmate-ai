"""
System heartbeat and presence nudges.

Heartbeat is responsible for health/status/maintenance ticks. Optional
presence messages are a low-priority side effect and are emitted separately
from chat messages so they do not enter conversation history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from pawmate.bridge.contracts import (
    HeartbeatStatusEvent,
    HeartbeatWarningEvent,
    MaintenanceTickEvent,
    PresenceNudgeEvent,
)
from pawmate.bridge.event_bus import event_bus


@dataclass(slots=True)
class HeartbeatSnapshot:
    timestamp: float
    sequence: int
    status: str
    worker: str
    engine: str
    model: str
    websocket: str
    task_running: bool
    task_duration_sec: int
    idle_seconds: int
    maintenance_due: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class HeartbeatController:
    """Periodic system health/status controller."""

    _FALLBACK_PRESENCE = "我在线，等你叫我。"

    def __init__(
        self,
        llm_client,
        is_engine_running: Callable[[], bool],
        interval_seconds: int = 1800,
        check_interval_seconds: int = 60,
        *,
        presence_enabled: bool = True,
        presence_after_heartbeats: tuple[int, int] = (55, 85),
        presence_min_gap_seconds: int = 55 * 60,
        long_task_notice_seconds: int = 180,
        maintenance_interval_seconds: int = 300,
        websocket_status: Callable[[], str] | None = None,
        model_status: Callable[[], str] | None = None,
        memory_context_provider: Callable[[], str] | None = None,
    ):
        self._llm = llm_client
        self._is_engine_running = is_engine_running
        self._interval = interval_seconds
        self._check_interval = check_interval_seconds
        self._presence_enabled = presence_enabled
        self._presence_after_heartbeats = presence_after_heartbeats
        self._presence_min_gap_seconds = max(0, int(presence_min_gap_seconds))
        self._long_task_notice_seconds = long_task_notice_seconds
        self._maintenance_interval_seconds = maintenance_interval_seconds
        self._websocket_status = websocket_status
        self._model_status_provider = model_status
        self._memory_context_provider = memory_context_provider

        self._last_activity: float = time.time()
        self._cooldown_until: float = 0.0
        self._cooldown_seconds: int = 10
        self._last_maintenance_at: float = time.time()
        self._last_long_task_notice_at: float = 0.0
        self._last_presence_at: float = 0.0
        self._task_started_at: float | None = None
        self._model_status: str = "available" if llm_client is not None else "unavailable"
        self._sequence: int = 0
        self._presence_countdown: int = self._new_presence_countdown()
        self._task: Optional[asyncio.Task] = None
        self._loop_running: bool = False

    def set_memory_context_provider(self, provider: Callable[[], str] | None) -> None:
        """Attach dynamic memory context for presence nudges."""
        self._memory_context_provider = provider

    def set_llm_client(self, llm_client) -> None:
        """Update the model client after provider retry or route changes."""
        self._llm = llm_client
        if llm_client is not None and self._model_status == "unavailable":
            self._model_status = "available"

    def poke(self) -> None:
        """Record user activity and suppress immediate presence nudges."""
        now = time.time()
        self._last_activity = now
        self._cooldown_until = now + self._cooldown_seconds
        self._presence_countdown = self._new_presence_countdown()

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if self._loop_running:
            return
        loop = loop or asyncio.get_event_loop()
        self._task = loop.create_task(self._loop())
        self._loop_running = True

    def stop(self) -> None:
        self._loop_running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def tick_once(self) -> HeartbeatSnapshot:
        """Run one heartbeat tick. Public for deterministic tests."""
        now = time.time()
        snapshot = self._build_snapshot(now)
        self._emit_status(snapshot)
        self._maybe_emit_long_task_notice(snapshot, now)
        self._maybe_run_maintenance(snapshot, now)
        await self._maybe_emit_presence(snapshot, now)
        return snapshot

    async def _loop(self) -> None:
        self._loop_running = True
        log = logging.getLogger("pawmate")
        try:
            while self._loop_running:
                await asyncio.sleep(self._check_interval)
                await self.tick_once()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("[Heartbeat] loop failed: %s", exc)
            self._emit_warning("heartbeat_error", f"Heartbeat loop failed: {exc}")
        finally:
            self._loop_running = False

    def _build_snapshot(self, now: float) -> HeartbeatSnapshot:
        running = bool(self._safe_call(self._is_engine_running, False))
        if running:
            if self._task_started_at is None:
                self._task_started_at = now
            task_duration = int(now - self._task_started_at)
            engine = "busy"
            status = "busy"
        else:
            self._task_started_at = None
            task_duration = 0
            idle_seconds = int(now - self._last_activity)
            engine = "idle" if idle_seconds >= self._interval else "ready"
            status = engine

        idle_seconds = int(now - self._last_activity)
        maintenance_due = now - self._last_maintenance_at >= self._maintenance_interval_seconds
        self._sequence += 1
        return HeartbeatSnapshot(
            timestamp=now,
            sequence=self._sequence,
            status=status,
            worker="alive",
            engine=engine,
            model=self._get_model_status(),
            websocket=self._get_websocket_status(),
            task_running=running,
            task_duration_sec=task_duration,
            idle_seconds=idle_seconds,
            maintenance_due=maintenance_due,
        )

    def _emit_status(self, snapshot: HeartbeatSnapshot) -> None:
        event_bus.publish(HeartbeatStatusEvent(snapshot.to_json()))

    def _maybe_emit_long_task_notice(self, snapshot: HeartbeatSnapshot, now: float) -> None:
        if not snapshot.task_running:
            return
        if snapshot.task_duration_sec < self._long_task_notice_seconds:
            return
        if now - self._last_long_task_notice_at < self._long_task_notice_seconds:
            return
        self._last_long_task_notice_at = now
        self._emit_warning(
            "task_running",
            f"Task is still running ({snapshot.task_duration_sec}s).",
            snapshot,
        )

    def _maybe_run_maintenance(self, snapshot: HeartbeatSnapshot, now: float) -> None:
        if not snapshot.maintenance_due:
            return
        self._last_maintenance_at = now
        for handler in logging.getLogger("pawmate").handlers:
            flush = getattr(handler, "flush", None)
            if callable(flush):
                flush()
        event_bus.publish(MaintenanceTickEvent(
            json.dumps(
                {
                    "timestamp": now,
                    "sequence": snapshot.sequence,
                    "actions": ["log_flush"],
                },
                ensure_ascii=False,
            )
        ))

    async def _maybe_emit_presence(self, snapshot: HeartbeatSnapshot, now: float) -> None:
        if not self._presence_enabled:
            return
        if snapshot.task_running or snapshot.status != "idle":
            return
        if now < self._cooldown_until:
            return
        if self._last_presence_at and now - self._last_presence_at < self._presence_min_gap_seconds:
            return
        self._presence_countdown -= 1
        if self._presence_countdown > 0:
            return
        text = await self._generate_presence_nudge(snapshot)
        self._presence_countdown = self._new_presence_countdown()
        if text:
            self._last_presence_at = now
            event_bus.publish(PresenceNudgeEvent(
                json.dumps(
                    {
                        "type": "idle_greeting",
                        "display": "chat",
                        "text": text,
                        "timestamp": now,
                        "status": snapshot.status,
                    },
                    ensure_ascii=False,
                )
            ))

    async def _generate_presence_nudge(self, snapshot: HeartbeatSnapshot) -> str:
        log = logging.getLogger("pawmate")
        payload = {
            "status": snapshot.status,
            "idle_seconds": snapshot.idle_seconds,
            "idle_minutes": max(1, round(snapshot.idle_seconds / 60)),
            "task_running": snapshot.task_running,
            "model": snapshot.model,
            "worker": snapshot.worker,
        }
        memory_context = self._get_memory_context()
        try:
            messages = [
                {
                    "role": "user",
                    "content": (
                        "Generate one calm PawMate presence nudge in Chinese. "
                        "Use status data plus memory context; never invent chat history. "
                        "If memory gives PawMate a nickname, persona, or naming preference, follow it. "
                        "Do not say 主人, 喵, 爪爪, 小爪, 摸摸头, or exact idle seconds. "
                        "Avoid pet-roleplay and repeated stock phrases. "
                        "If there is no useful context, simply say you are online and waiting. "
                        "Length target: 20-60 Chinese characters.\n\n"
                        f"Status data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        f"Memory context:\n{memory_context or '- none'}"
                    ),
                }
            ]
            collected: list[str] = []
            async for event in self._llm.stream(
                messages=messages,
                system=(
                    "You write brief non-intrusive desktop companion status nudges. "
                    "Return only the message text."
                ),
                max_tokens=120,
            ):
                if event.get("type") == "text_delta":
                    collected.append(event.get("text", ""))
            text = self._clean_presence_text("".join(collected))
            if text:
                self._model_status = "available"
                return text
        except Exception as exc:
            self._model_status = "unavailable"
            log.warning("[Heartbeat] presence generation failed: %s", exc)
        return self._FALLBACK_PRESENCE

    def _get_memory_context(self) -> str:
        if self._memory_context_provider is None:
            return ""
        try:
            text = str(self._memory_context_provider() or "").strip()
            return text[:2400]
        except Exception as exc:
            logging.getLogger("pawmate").warning("[Heartbeat] memory context unavailable: %s", exc)
            return ""

    def _get_model_status(self) -> str:
        if self._model_status_provider is not None:
            return str(self._safe_call(self._model_status_provider, self._model_status))
        return self._model_status

    def _get_websocket_status(self) -> str:
        if self._websocket_status is not None:
            return str(self._safe_call(self._websocket_status, "unknown"))
        return "local-ui"

    def _emit_warning(
        self,
        kind: str,
        message: str,
        snapshot: HeartbeatSnapshot | None = None,
    ) -> None:
        event_bus.publish(HeartbeatWarningEvent(
            json.dumps(
                {
                    "type": kind,
                    "message": message,
                    "snapshot": asdict(snapshot) if snapshot else None,
                    "timestamp": time.time(),
                },
                ensure_ascii=False,
            )
        ))

    def _new_presence_countdown(self) -> int:
        low, high = self._presence_after_heartbeats
        low = max(1, int(low))
        high = max(low, int(high))
        return random.randint(low, high)

    @staticmethod
    def _clean_presence_text(text: str) -> str:
        return (text or "").strip().strip("\"'“”‘’")

    @staticmethod
    def _safe_call(fn: Callable[[], object], default: object) -> object:
        try:
            return fn()
        except Exception:
            return default
