"""Domain models for browser automation workflows and runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class WorkflowStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    APPROVAL_REQUIRED = "approval_required"


class StepStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_ms: int = 250
    retry_on: tuple[str, ...] = (
        "timeout",
        "selector_not_found",
        "stale_ref",
        "browser_dependency_error",
    )

    @classmethod
    def from_dict(cls, value: Any) -> "RetryPolicy":
        if not isinstance(value, dict):
            return cls()
        retry_on = value.get("retry_on", cls.retry_on)
        return cls(
            max_attempts=int(value.get("max_attempts", 1)),
            backoff_ms=int(value.get("backoff_ms", 250)),
            retry_on=tuple(str(item) for item in retry_on) if isinstance(retry_on, list) else cls.retry_on,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_ms": self.backoff_ms,
            "retry_on": list(self.retry_on),
        }


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30_000
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    on_error: str = "stop"
    save_as: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowStep":
        return cls(
            id=str(value.get("id") or ""),
            action=str(value.get("action") or ""),
            params=dict(value.get("params") or {}),
            timeout_ms=int(value.get("timeout_ms", 30_000)),
            retry=RetryPolicy.from_dict(value.get("retry")),
            on_error=str(value.get("on_error") or "stop"),
            save_as=str(value.get("save_as") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            "params": self.params,
            "timeout_ms": self.timeout_ms,
            "retry": self.retry.to_dict(),
            "on_error": self.on_error,
        }
        if self.save_as:
            result["save_as"] = self.save_as
        return result


@dataclass(frozen=True)
class WorkflowInputSpec:
    type: str = "string"
    required: bool = False
    default: Any = None
    secret: bool = False
    description: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> "WorkflowInputSpec":
        if not isinstance(value, dict):
            return cls()
        return cls(
            type=str(value.get("type") or "string"),
            required=bool(value.get("required", False)),
            default=value.get("default"),
            secret=bool(value.get("secret", False)),
            description=str(value.get("description") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "type": self.type,
            "required": self.required,
            "secret": self.secret,
        }
        if self.default is not None:
            result["default"] = self.default
        if self.description:
            result["description"] = self.description
        return result


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    name: str
    version: int
    steps: tuple[WorkflowStep, ...]
    description: str = ""
    status: WorkflowStatus = WorkflowStatus.DRAFT
    inputs: dict[str, WorkflowInputSpec] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, workflow_id: str = "") -> "WorkflowDefinition":
        status_text = str(value.get("status") or WorkflowStatus.DRAFT.value)
        return cls(
            id=workflow_id or str(value.get("id") or ""),
            name=str(value.get("name") or ""),
            version=int(value.get("version", 1)),
            description=str(value.get("description") or ""),
            status=WorkflowStatus(status_text),
            inputs={
                str(key): WorkflowInputSpec.from_dict(item)
                for key, item in dict(value.get("inputs") or {}).items()
            },
            steps=tuple(WorkflowStep.from_dict(item) for item in list(value.get("steps") or [])),
            created_at=str(value.get("created_at") or utc_now()),
            updated_at=str(value.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "status": self.status.value,
            "inputs": {key: value.to_dict() for key, value in self.inputs.items()},
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "issues": [issue.to_dict() for issue in self.issues]}


@dataclass(frozen=True)
class RiskDecision:
    level: str
    requires_approval: bool
    critical: bool = False
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "requires_approval": self.requires_approval,
            "critical": self.critical,
            "reasons": list(self.reasons),
        }


@dataclass
class RunRecord:
    id: str
    workflow_id: str
    workflow_version: int
    status: RunStatus = RunStatus.QUEUED
    mode: str = "workflow"
    dry_run: bool = False
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    current_step: str = ""
    completed_steps: list[str] = field(default_factory=list)
    error: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    started_at: str = ""
    finished_at: str = ""
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunRecord":
        return cls(
            id=str(value.get("id") or ""),
            workflow_id=str(value.get("workflow_id") or ""),
            workflow_version=int(value.get("workflow_version", 1)),
            status=RunStatus(str(value.get("status") or RunStatus.QUEUED.value)),
            mode=str(value.get("mode") or "workflow"),
            dry_run=bool(value.get("dry_run", False)),
            inputs=dict(value.get("inputs") or {}),
            outputs=dict(value.get("outputs") or {}),
            current_step=str(value.get("current_step") or ""),
            completed_steps=list(value.get("completed_steps") or []),
            error=dict(value.get("error") or {}),
            approval=dict(value.get("approval") or {}),
            created_at=str(value.get("created_at") or utc_now()),
            started_at=str(value.get("started_at") or ""),
            finished_at=str(value.get("finished_at") or ""),
            updated_at=str(value.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "status": self.status.value,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "current_step": self.current_step,
            "completed_steps": self.completed_steps,
            "error": self.error,
            "approval": self.approval,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class TraceEvent:
    run_id: str
    event: str
    timestamp: str = field(default_factory=utc_now)
    step_id: str = ""
    step_path: str = ""
    attempt: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event": self.event,
            "timestamp": self.timestamp,
            "step_id": self.step_id,
            "step_path": self.step_path,
            "attempt": self.attempt,
            "data": self.data,
        }
