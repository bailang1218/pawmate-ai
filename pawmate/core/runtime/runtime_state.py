"""Agent run, turn, and resource lock state contracts."""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator


class ResourceKind(str, Enum):
    BROWSER_SESSION = "BROWSER_SESSION"
    FILE_PATH = "FILE_PATH"
    WORKSPACE = "WORKSPACE"
    SHELL_PROCESS = "SHELL_PROCESS"
    MEMORY_STORE = "MEMORY_STORE"
    NETWORK_SESSION = "NETWORK_SESSION"
    UI_STATE = "UI_STATE"


@dataclass(frozen=True, order=True)
class ResourceKey:
    kind: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name}


@dataclass
class TurnState:
    turn_id: str
    started_at: float = field(default_factory=time.time)
    user_seq: int | None = None
    last_status: str = "running"
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "started_at": self.started_at,
            "user_seq": self.user_seq,
            "last_status": self.last_status,
            "last_error": self.last_error,
        }


@dataclass
class AgentRunState:
    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex}")
    started_at: float = field(default_factory=time.time)
    current_turn: TurnState | None = None
    last_status: str = "idle"
    last_error: str = ""

    def start_turn(self, turn_id: str) -> TurnState:
        self.current_turn = TurnState(turn_id=str(turn_id))
        self.last_status = "running"
        self.last_error = ""
        return self.current_turn

    def finish_turn(self, status: str, error: str = "") -> None:
        self.last_status = status
        self.last_error = error
        if self.current_turn is not None:
            self.current_turn.last_status = status
            self.current_turn.last_error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "current_turn": self.current_turn.to_dict() if self.current_turn else None,
        }


class ResourceLockManager:
    def __init__(self) -> None:
        self._locks: dict[ResourceKey, asyncio.Lock] = {}
        self._owners: dict[ResourceKey, str] = {}
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def hold_many(self, resources: list[ResourceKey], *, owner: str) -> AsyncIterator[None]:
        ordered = sorted(set(resources))
        acquired: list[ResourceKey] = []
        try:
            for resource in ordered:
                lock = await self._get_lock(resource)
                await lock.acquire()
                self._owners[resource] = owner
                acquired.append(resource)
            yield
        finally:
            for resource in reversed(acquired):
                self._owners.pop(resource, None)
                self._locks[resource].release()

    def snapshot(self) -> dict[str, Any]:
        return {
            "held": [
                {"resource": key.to_dict(), "owner": owner}
                for key, owner in sorted(self._owners.items(), key=lambda item: item[0])
            ]
        }

    async def _get_lock(self, resource: ResourceKey) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(resource)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[resource] = lock
            return lock


def infer_tool_resources(tool_name: str, tool_input: dict[str, Any]) -> list[ResourceKey]:
    name = str(tool_name or "")
    data = tool_input or {}
    resources: list[ResourceKey] = []
    if name.startswith("browser_") or name.startswith("native_browser_"):
        resources.append(ResourceKey(ResourceKind.BROWSER_SESSION.value, "active"))
    if name in {"run_powershell_query", "run_shell_command", "run_command", "run_script", "run_tests"}:
        resources.append(ResourceKey(ResourceKind.SHELL_PROCESS.value, str(data.get("cwd") or "workspace")))
    if name.startswith("core_") or name in {
        "remember_memory", "consider_memory", "forget_memory", "take_note", "search_memory",
        "search_history", "open_history_context", "growth_status",
    }:
        resources.append(ResourceKey(ResourceKind.MEMORY_STORE.value, "local"))
    if name in {"download_file", "download_with_metadata", "open_url"}:
        resources.append(ResourceKey(ResourceKind.NETWORK_SESSION.value, "external"))

    for key in _tool_path_argument_names(name):
        value = data.get(key)
        if not value:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not item:
                continue
            try:
                path = str(Path(str(item)).expanduser().resolve())
            except Exception:
                path = str(item)
            resources.append(ResourceKey(ResourceKind.FILE_PATH.value, path))
    return resources


def _tool_path_argument_names(tool_name: str) -> tuple[str, ...]:
    if tool_name in {"write_file", "read_file", "read_text_file", "read_symbol", "create_directory"}:
        return ("path",)
    if tool_name == "move_path":
        return ("source", "destination")
    if tool_name == "move_paths":
        return ("sources", "destination_dir")
    if tool_name in {"download_file", "download_with_metadata"}:
        return ("destination_folder", "filename")
    if tool_name in {"capture_screenshot", "ocr_image", "vision_analyze"}:
        return ("path",)
    return ()
