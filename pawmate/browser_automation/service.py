"""Application service coordinating workflows, runs, AI planners, and persistence."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .ai import AgentAutomationRunner, AgentRunOptions, SchemaAgentPlanner, SchemaWorkflowGenerator, WorkflowGenerator
from .executor import WorkflowExecutor
from .models import RunRecord, RunStatus, TraceEvent, WorkflowDefinition, WorkflowStatus, utc_now
from .policy import BrowserAutomationPolicy
from .runtime_adapter import BrowserRuntime, PawMateBrowserRuntime
from .store import AutomationRunLease, AutomationStore
from .validation import WorkflowValidator


class AutomationServiceError(RuntimeError):
    code = "service_error"
    http_status = 400

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = details


class NotFoundError(AutomationServiceError):
    code = "not_found"
    http_status = 404


class ConflictError(AutomationServiceError):
    code = "conflict"
    http_status = 409


class ValidationError(AutomationServiceError):
    code = "validation_error"
    http_status = 422


class BrowserAutomationService:
    def __init__(
        self,
        *,
        store: AutomationStore | None = None,
        runtime: BrowserRuntime | None = None,
        generator: WorkflowGenerator | None = None,
    ):
        self.store = store or AutomationStore()
        self.runtime = runtime or PawMateBrowserRuntime()
        self.policy = BrowserAutomationPolicy()
        self.validator = WorkflowValidator()
        self.executor = WorkflowExecutor(self.store, self.runtime, self.policy)
        self.generator = generator or SchemaWorkflowGenerator()
        self.agent_runner = AgentAutomationRunner(self.store, self.runtime, SchemaAgentPlanner(), self.policy)
        self._active_task: asyncio.Task[RunRecord] | None = None
        self._active_run_id = ""
        self._run_lease: AutomationRunLease | None = None
        self._task_guard = asyncio.Lock()
        self._recover_interrupted_runs()

    def validate_workflow(self, value: Any) -> dict[str, Any]:
        return self.validator.validate(value).to_dict()

    def create_workflow(self, value: dict[str, Any]) -> WorkflowDefinition:
        payload = dict(value)
        workflow_id = str(payload.get("id") or f"wf_{uuid.uuid4().hex[:16]}")
        payload["id"] = workflow_id
        report = self.validator.validate(payload)
        if not report.valid:
            raise ValidationError("Workflow validation failed", details=report.to_dict())
        if self.store.get_workflow(workflow_id) is not None:
            raise ConflictError(f"Workflow already exists: {workflow_id}")
        now = utc_now()
        payload["created_at"] = now
        payload["updated_at"] = now
        workflow = WorkflowDefinition.from_dict(payload)
        self.store.save_workflow(workflow)
        return workflow

    def update_workflow(self, workflow_id: str, value: dict[str, Any]) -> WorkflowDefinition:
        existing = self.get_workflow(workflow_id)
        payload = dict(value)
        payload["id"] = workflow_id
        payload.setdefault("version", existing.version + 1)
        if not isinstance(payload.get("version"), int) or payload["version"] <= existing.version:
            raise ValidationError(f"Updated workflow version must be greater than {existing.version}")
        payload["created_at"] = existing.created_at
        payload["updated_at"] = utc_now()
        report = self.validator.validate(payload)
        if not report.valid:
            raise ValidationError("Workflow validation failed", details=report.to_dict())
        workflow = WorkflowDefinition.from_dict(payload)
        self.store.save_workflow(workflow)
        return workflow

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition:
        try:
            workflow = self.store.get_workflow(workflow_id)
        except ValueError as exc:
            raise NotFoundError("Workflow not found") from exc
        if workflow is None:
            raise NotFoundError(f"Workflow not found: {workflow_id}")
        return workflow

    def list_workflows(self) -> list[WorkflowDefinition]:
        return self.store.list_workflows()

    def get_workflow_version(self, workflow_id: str, version: int) -> WorkflowDefinition:
        self.get_workflow(workflow_id)
        try:
            workflow = self.store.get_workflow_version(workflow_id, version)
        except ValueError as exc:
            raise NotFoundError("Workflow version not found") from exc
        if workflow is None:
            raise NotFoundError(f"Workflow version not found: {workflow_id}@{version}")
        return workflow

    async def generate_workflow(
        self,
        goal: str,
        context: dict[str, Any],
        *,
        save: bool = False,
        allow_model_data: bool = False,
    ) -> dict[str, Any]:
        if not allow_model_data:
            raise ValidationError("AI generation requires explicit allow_model_data=true")
        generated = await self.generator.generate(goal, context)
        report = self.validator.validate(generated)
        result = {"workflow": generated, "validation": report.to_dict(), "saved": False}
        if save:
            if not report.valid:
                raise ValidationError("Generated workflow failed validation", details=result)
            workflow = self.create_workflow(generated)
            result.update(workflow=workflow.to_dict(), saved=True)
        return result

    async def start_workflow_run(
        self,
        workflow_id: str,
        *,
        inputs: dict[str, Any] | None = None,
        dry_run: bool = False,
        allow_high_risk: bool = False,
        allow_critical: bool = False,
        approval: dict[str, Any] | None = None,
    ) -> RunRecord:
        workflow = self.get_workflow(workflow_id)
        if workflow.status == WorkflowStatus.ARCHIVED:
            raise ConflictError("Archived workflows cannot run")
        actual_inputs = dict(inputs or {})
        run = RunRecord(
            id=f"run_{uuid.uuid4().hex[:20]}",
            workflow_id=workflow.id,
            workflow_version=workflow.version,
            dry_run=bool(dry_run),
            inputs=self._redacted_inputs(workflow, actual_inputs),
        )
        if not dry_run:
            run.approval = self._validate_approval(allow_high_risk, allow_critical, approval)
        async with self._task_guard:
            self._ensure_idle()
            self._acquire_run_lease()
            try:
                self.store.save_run(run)
                self.executor.register_run(run.id)
                self._active_run_id = run.id
                self._active_task = asyncio.create_task(
                    self.executor.execute(
                        run,
                        workflow,
                        actual_inputs,
                        allow_high_risk=allow_high_risk,
                        allow_critical=allow_critical,
                    ),
                    name=f"browser-workflow-{run.id}",
                )
                self._active_task.add_done_callback(self._task_finished)
            except Exception:
                self._release_run_lease()
                raise
        return run

    async def start_agent_run(
        self,
        goal: str,
        *,
        max_turns: int = 20,
        step_timeout_ms: int = 30_000,
        allow_high_risk: bool = False,
        allow_critical: bool = False,
        allow_model_data: bool = False,
        visibility: str = "auto",
        approval: dict[str, Any] | None = None,
    ) -> RunRecord:
        if not allow_model_data:
            raise ValidationError("AI direct operation requires explicit allow_model_data=true")
        clean_goal = str(goal or "").strip()
        if not 1 <= len(clean_goal) <= 4000:
            raise ValidationError("goal must contain 1..4000 characters")
        if not 1 <= max_turns <= 50:
            raise ValidationError("max_turns must be 1..50")
        if not 100 <= step_timeout_ms <= 120_000:
            raise ValidationError("step_timeout_ms must be 100..120000")
        if visibility not in {"auto", "background", "foreground"}:
            raise ValidationError("visibility must be auto, background, or foreground")
        run = RunRecord(
            id=f"run_{uuid.uuid4().hex[:20]}",
            workflow_id="agent_direct",
            workflow_version=1,
            mode="agent",
            inputs={"goal": "[REDACTED]"},
        )
        run.approval = self._validate_approval(allow_high_risk, allow_critical, approval)
        options = AgentRunOptions(
            max_turns=max_turns,
            step_timeout_ms=step_timeout_ms,
            allow_high_risk=allow_high_risk,
            allow_critical=allow_critical,
            visibility=visibility,
        )
        async with self._task_guard:
            self._ensure_idle()
            self._acquire_run_lease()
            try:
                self.store.save_run(run)
                self.agent_runner.register_run(run.id)
                self._active_run_id = run.id
                self._active_task = asyncio.create_task(
                    self.agent_runner.run(run, clean_goal, options),
                    name=f"browser-agent-{run.id}",
                )
                self._active_task.add_done_callback(self._task_finished)
            except Exception:
                self._release_run_lease()
                raise
        return run

    def get_run(self, run_id: str) -> RunRecord:
        try:
            run = self.store.get_run(run_id)
        except ValueError as exc:
            raise NotFoundError("Run not found") from exc
        if run is None:
            raise NotFoundError(f"Run not found: {run_id}")
        return run

    def list_runs(self, *, limit: int = 100) -> list[RunRecord]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValidationError("limit must be 1..500")
        runs = self.store.list_runs()
        runs.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return runs[:limit]

    def get_trace(self, run_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        self.get_run(run_id)
        return self.store.read_trace(run_id, limit=limit)

    def cancel_run(self, run_id: str) -> bool:
        self.get_run(run_id)
        if run_id != self._active_run_id:
            return False
        return self.executor.cancel(run_id) or self.agent_runner.cancel(run_id)

    async def wait_run(self, run_id: str, *, timeout: float = 60) -> RunRecord:
        task = self._active_task if run_id == self._active_run_id else None
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return self.get_run(run_id)

    async def close(self) -> None:
        task = self._active_task
        if task is None or task.done():
            self._release_run_lease()
            return
        self.cancel_run(self._active_run_id)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._release_run_lease()

    @property
    def busy(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    @property
    def active_run_id(self) -> str:
        return self._active_run_id if self.busy else ""

    def _ensure_idle(self) -> None:
        if self.busy:
            raise ConflictError(f"Browser automation is busy with run {self._active_run_id}")

    def _task_finished(self, task: asyncio.Task[RunRecord]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._release_run_lease()

    def _acquire_run_lease(self) -> None:
        lease = self.store.try_acquire_run_lease()
        if lease is None:
            raise ConflictError("Browser automation runtime is busy in another PawMate process")
        self._run_lease = lease
        try:
            self._recover_interrupted_runs_locked()
        except Exception:
            self._release_run_lease()
            raise

    def _release_run_lease(self) -> None:
        lease = self._run_lease
        self._run_lease = None
        if lease is not None:
            lease.release()

    def _recover_interrupted_runs(self) -> None:
        lease = self.store.try_acquire_run_lease()
        if lease is None:
            return
        try:
            self._recover_interrupted_runs_locked()
        finally:
            lease.release()

    def _recover_interrupted_runs_locked(self) -> None:
        for run in self.store.list_runs():
            if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                continue
            run.status = RunStatus.FAILED
            run.current_step = ""
            run.error = {
                "code": "process_interrupted",
                "message": "The previous process stopped before this browser run completed; it was not replayed automatically.",
            }
            run.finished_at = utc_now()
            run.updated_at = run.finished_at
            self.store.save_run(run)
            self.store.append_trace(
                TraceEvent(
                    run_id=run.id,
                    event="run_recovered_as_interrupted",
                    data={"error": run.error, "completed_steps": list(run.completed_steps)},
                )
            )

    @staticmethod
    def _validate_approval(
        allow_high_risk: bool,
        allow_critical: bool,
        approval: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not allow_high_risk and not allow_critical:
            return {}
        if not isinstance(approval, dict) or approval.get("approved") is not True:
            raise ValidationError("Risk override requires approval.approved=true")
        reason = str(approval.get("reason") or "").strip()
        approved_by = str(approval.get("approved_by") or "").strip()
        if not 3 <= len(reason) <= 500:
            raise ValidationError("approval.reason must contain 3..500 characters")
        if not 1 <= len(approved_by) <= 100:
            raise ValidationError("approval.approved_by must contain 1..100 characters")
        if allow_critical and approval.get("scope") != "critical":
            raise ValidationError("Critical risk override requires approval.scope='critical'")
        return {
            "granted": True,
            "approved_by": approved_by,
            "reason": reason,
            "scope": "critical" if allow_critical else "high",
        }

    @staticmethod
    def _redacted_inputs(workflow: WorkflowDefinition, inputs: dict[str, Any]) -> dict[str, Any]:
        result = dict(inputs)
        for name, spec in workflow.inputs.items():
            if spec.secret and name in result:
                result[name] = "[REDACTED]"
        return result
