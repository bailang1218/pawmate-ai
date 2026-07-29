"""Filesystem persistence for workflows, run checkpoints, and JSONL traces."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pawmate.storage.app_paths import get_app_paths

from .models import RunRecord, TraceEvent, WorkflowDefinition


SAFE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,127}$")


class AutomationRunLease:
    """One-byte OS file lock preventing concurrent browser runs across processes."""

    def __init__(self, handle: Any):
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()


class AutomationStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or get_app_paths().browser_automation_dir).resolve()
        self.workflows_dir = self.root / "workflows"
        self.workflow_versions_dir = self.root / "workflow_versions"
        self.runs_dir = self.root / "runs"
        self.traces_dir = self.root / "traces"
        for path in (self.workflows_dir, self.workflow_versions_dir, self.runs_dir, self.traces_dir):
            path.mkdir(parents=True, exist_ok=True)

    def try_acquire_run_lease(self) -> AutomationRunLease | None:
        lock_path = self.root / "browser_runtime.lock"
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return AutomationRunLease(handle)
        except OSError:
            handle.close()
            return None

    def save_workflow(self, workflow: WorkflowDefinition) -> None:
        version_dir = self.workflow_versions_dir / self._safe_id(workflow.id)
        version_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_json(version_dir / f"v{workflow.version}.json", workflow.to_dict())
        self._atomic_json(self._path(self.workflows_dir, workflow.id, ".json"), workflow.to_dict())

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition | None:
        value = self._read_json(self._path(self.workflows_dir, workflow_id, ".json"))
        return WorkflowDefinition.from_dict(value) if value is not None else None

    def list_workflows(self) -> list[WorkflowDefinition]:
        workflows: list[WorkflowDefinition] = []
        for path in sorted(self.workflows_dir.glob("*.json")):
            try:
                workflows.append(WorkflowDefinition.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return workflows

    def get_workflow_version(self, workflow_id: str, version: int) -> WorkflowDefinition | None:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("Invalid workflow version")
        version_dir = self.workflow_versions_dir / self._safe_id(workflow_id)
        value = self._read_json(version_dir / f"v{version}.json")
        return WorkflowDefinition.from_dict(value) if value is not None else None

    def save_run(self, run: RunRecord) -> None:
        self._atomic_json(self._path(self.runs_dir, run.id, ".json"), run.to_dict())

    def get_run(self, run_id: str) -> RunRecord | None:
        value = self._read_json(self._path(self.runs_dir, run_id, ".json"))
        return RunRecord.from_dict(value) if value is not None else None

    def list_runs(self) -> list[RunRecord]:
        runs: list[RunRecord] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                runs.append(RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return runs

    def append_trace(self, event: TraceEvent) -> None:
        path = self._path(self.traces_dir, event.run_id, ".jsonl")
        encoded = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 128 * 1024:
            encoded = json.dumps(
                {
                    **event.to_dict(),
                    "data": {"truncated": True, "message": "Trace event exceeded 128 KiB"},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()

    def read_trace(self, run_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        path = self._path(self.traces_dir, run_id, ".jsonl")
        if not path.exists():
            return []
        result: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(result) >= max(1, min(limit, 5000)):
                    break
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    result.append(value)
        return result

    @staticmethod
    def _path(directory: Path, item_id: str, suffix: str) -> Path:
        safe_id = AutomationStore._safe_id(item_id)
        return directory / f"{safe_id}{suffix}"

    @staticmethod
    def _safe_id(item_id: str) -> str:
        if not SAFE_ID_RE.fullmatch(str(item_id or "")):
            raise ValueError("Unsafe automation identifier")
        return str(item_id)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected object in {path.name}")
        return value

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
