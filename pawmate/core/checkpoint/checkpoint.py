"""Durable checkpoints for agent run recovery."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pawmate.core.safety.redaction import redact_sensitive_data
from pawmate.storage.app_paths import get_app_paths


class CheckpointStage(str, Enum):
    TURN_STARTED = "turn_started"
    CONFIRM_PENDING = "confirm_pending"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    PROVIDER_ERROR = "provider_error"
    TURN_FAILED = "turn_failed"
    TURN_FINISHED = "turn_finished"


@dataclass(frozen=True)
class AgentCheckpoint:
    checkpoint_id: str
    run_id: str
    turn_id: str
    stage: CheckpointStage
    timestamp: float = field(default_factory=time.time)
    tool_call_id: str = ""
    tool_name: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    side_effect: str = ""
    idempotency_key: str = ""
    replay_allowed: bool = False

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        turn_id: str,
        stage: CheckpointStage | str,
        tool_call_id: str = "",
        tool_name: str = "",
        state: dict[str, Any] | None = None,
        side_effect: str = "",
        idempotency_key: str = "",
        replay_allowed: bool = False,
    ) -> "AgentCheckpoint":
        return cls(
            checkpoint_id=f"ckpt_{uuid.uuid4().hex}",
            run_id=str(run_id or ""),
            turn_id=str(turn_id or ""),
            stage=CheckpointStage(stage),
            tool_call_id=str(tool_call_id or ""),
            tool_name=str(tool_name or ""),
            state=_json_safe(redact_sensitive_data(state or {})),
            side_effect=str(side_effect or ""),
            idempotency_key=str(idempotency_key or ""),
            replay_allowed=bool(replay_allowed),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "stage": self.stage.value,
            "timestamp": self.timestamp,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "state": self.state,
            "side_effect": self.side_effect,
            "idempotency_key": self.idempotency_key,
            "replay_allowed": self.replay_allowed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentCheckpoint":
        return cls(
            checkpoint_id=str(payload.get("checkpoint_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            turn_id=str(payload.get("turn_id") or ""),
            stage=CheckpointStage(payload.get("stage") or CheckpointStage.TURN_STARTED.value),
            timestamp=float(payload.get("timestamp") or 0),
            tool_call_id=str(payload.get("tool_call_id") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            state=payload.get("state") if isinstance(payload.get("state"), dict) else {},
            side_effect=str(payload.get("side_effect") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            replay_allowed=bool(payload.get("replay_allowed", False)),
        )


class CheckpointStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (get_app_paths().data_root / "checkpoints")

    def save(self, checkpoint: AgentCheckpoint) -> Path:
        run_dir = self.root / _safe_name(checkpoint.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{time.time_ns():020d}-{_safe_name(checkpoint.checkpoint_id)}.json"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        latest = run_dir / "latest.json"
        latest_tmp = run_dir / "latest.tmp"
        latest_tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        latest_tmp.replace(latest)
        return path

    def load_latest(self, run_id: str) -> AgentCheckpoint | None:
        path = self.root / _safe_name(run_id) / "latest.json"
        if not path.exists():
            return None
        return AgentCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_checkpoints(self, run_id: str) -> list[AgentCheckpoint]:
        run_dir = self.root / _safe_name(run_id)
        if not run_dir.exists():
            return []
        checkpoint_files = []
        for path in run_dir.glob("*.json"):
            if path.name == "latest.json":
                continue
            checkpoint = AgentCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
            try:
                saved_ns = path.stat().st_mtime_ns
            except OSError:
                saved_ns = 0
            checkpoint_files.append((checkpoint.timestamp, saved_ns, path.name, checkpoint))
        checkpoint_files.sort(key=lambda item: item[:3])
        return [item[3] for item in checkpoint_files]


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _safe_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or ""))
    return text[:80] or "run"
