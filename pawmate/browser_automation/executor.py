"""Deterministic, checkpointed browser workflow executor."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .models import RunRecord, RunStatus, TraceEvent, WorkflowDefinition, WorkflowStep, utc_now
from .policy import BrowserAutomationPolicy, PolicyViolation
from .runtime_adapter import BrowserRuntime
from .store import AutomationStore
from .templating import TemplateResolutionError, evaluate_condition, resolve_value


class WorkflowExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.result = result or {}


class WorkflowCancelled(WorkflowExecutionError):
    def __init__(self):
        super().__init__("cancelled", "Workflow run was cancelled")


class WorkflowExecutor:
    def __init__(self, store: AutomationStore, runtime: BrowserRuntime, policy: BrowserAutomationPolicy | None = None):
        self.store = store
        self.runtime = runtime
        self.policy = policy or BrowserAutomationPolicy()
        self._run_lock = asyncio.Lock()
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._secret_values: dict[str, set[str]] = {}

    @property
    def busy(self) -> bool:
        return self._run_lock.locked()

    def register_run(self, run_id: str) -> None:
        self._cancel_events.setdefault(run_id, asyncio.Event())

    def cancel(self, run_id: str) -> bool:
        event = self._cancel_events.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    async def execute(
        self,
        run: RunRecord,
        workflow: WorkflowDefinition,
        inputs: dict[str, Any],
        *,
        allow_high_risk: bool = False,
        allow_critical: bool = False,
    ) -> RunRecord:
        cancel_event = self._cancel_events.setdefault(run.id, asyncio.Event())
        async with self._run_lock:
            try:
                self._start(run)
                context = self._build_context(workflow, inputs)
                self._secret_values[run.id] = {
                    str(context.get(name))
                    for name, spec in workflow.inputs.items()
                    if spec.secret and context.get(name) not in (None, "")
                }
                if run.dry_run:
                    run.outputs["plan"] = _scrub_secrets(
                        self._plan(workflow.steps, context),
                        self._secret_values.get(run.id, set()),
                    )
                    self._finish(run, RunStatus.SUCCEEDED)
                    self._trace(run, "dry_run_completed", data={"steps": len(run.outputs["plan"])})
                    return run
                blocked = self._preflight(workflow.steps, context, allow_high_risk, allow_critical)
                if blocked:
                    run.status = RunStatus.APPROVAL_REQUIRED
                    run.approval = {"required": True, "steps": blocked}
                    run.finished_at = utc_now()
                    self._checkpoint(run)
                    self._trace(run, "approval_required", data=run.approval)
                    return run

                await self._execute_steps(
                    list(workflow.steps),
                    run,
                    context,
                    cancel_event,
                    path_prefix="steps",
                    allow_high_risk=allow_high_risk,
                    allow_critical=allow_critical,
                )
                run.outputs = _bounded_mapping(
                    _scrub_secrets(context["steps"], self._secret_values.get(run.id, set()))
                )
                self._finish(run, RunStatus.SUCCEEDED)
                self._trace(run, "run_succeeded", data={"completed_steps": list(run.completed_steps)})
                return run
            except WorkflowCancelled as exc:
                run.error = {"code": exc.code, "message": str(exc)}
                self._finish(run, RunStatus.CANCELLED)
                self._trace(run, "run_cancelled", data=run.error)
                return run
            except asyncio.CancelledError:
                run.error = {"code": "cancelled", "message": "Workflow task was cancelled during shutdown"}
                self._finish(run, RunStatus.CANCELLED)
                self._trace(run, "run_cancelled", data=run.error)
                return run
            except (WorkflowExecutionError, TemplateResolutionError, PolicyViolation) as exc:
                code = getattr(exc, "code", "workflow_error")
                run.error = {
                    "code": code,
                    "message": _scrub_secrets(str(exc), self._secret_values.get(run.id, set())),
                }
                if isinstance(exc, WorkflowExecutionError) and exc.result:
                    run.error["result"] = _bounded_value(
                        _scrub_secrets(exc.result, self._secret_values.get(run.id, set()))
                    )
                status = RunStatus.APPROVAL_REQUIRED if code in {"approval_required", "critical_approval_required"} else RunStatus.FAILED
                self._finish(run, status)
                self._trace(run, "run_failed", data=run.error)
                return run
            except Exception as exc:
                run.error = {
                    "code": "internal_error",
                    "message": _scrub_secrets(str(exc), self._secret_values.get(run.id, set())),
                }
                self._finish(run, RunStatus.FAILED)
                self._trace(run, "run_failed", data=run.error)
                return run
            finally:
                self._cancel_events.pop(run.id, None)
                self._secret_values.pop(run.id, None)

    def _start(self, run: RunRecord) -> None:
        run.status = RunStatus.RUNNING
        run.started_at = utc_now()
        run.updated_at = run.started_at
        self._checkpoint(run)
        self._trace(run, "run_started", data={"workflow_id": run.workflow_id, "dry_run": run.dry_run})

    def _finish(self, run: RunRecord, status: RunStatus) -> None:
        run.status = status
        run.current_step = ""
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        self._checkpoint(run)

    async def _execute_steps(
        self,
        steps: list[WorkflowStep],
        run: RunRecord,
        context: dict[str, Any],
        cancel_event: asyncio.Event,
        *,
        path_prefix: str,
        allow_high_risk: bool,
        allow_critical: bool,
    ) -> None:
        for index, step in enumerate(steps):
            if cancel_event.is_set():
                raise WorkflowCancelled()
            step_path = f"{path_prefix}[{index}]"
            try:
                result = await self._execute_step(
                    step,
                    run,
                    context,
                    cancel_event,
                    step_path=step_path,
                    allow_high_risk=allow_high_risk,
                    allow_critical=allow_critical,
                )
            except WorkflowExecutionError as exc:
                if step.on_error != "continue":
                    raise
                result = {"ok": False, "error_type": exc.code, "message": str(exc), "continued": True}
                self._trace(run, "step_error_continued", step=step, step_path=step_path, data=_bounded_value(result))
            context["steps"][step.id] = result
            if step.save_as:
                context[step.save_as] = result
            run.completed_steps.append(step_path)
            run.outputs = _bounded_mapping(
                _scrub_secrets(context["steps"], self._secret_values.get(run.id, set()))
            )
            self._checkpoint(run)

    async def _execute_step(
        self,
        step: WorkflowStep,
        run: RunRecord,
        context: dict[str, Any],
        cancel_event: asyncio.Event,
        *,
        step_path: str,
        allow_high_risk: bool,
        allow_critical: bool,
    ) -> dict[str, Any]:
        run.current_step = step_path
        run.updated_at = utc_now()
        self._checkpoint(run)
        params = _resolve_step_params(step, context)
        if not isinstance(params, dict):
            raise WorkflowExecutionError("invalid_params", "Resolved step params must be an object")
        decision = self.policy.assess(step, params)
        self.policy.authorize(decision, allow_high_risk=allow_high_risk, allow_critical=allow_critical)
        if step.action == "browser.goto":
            self.policy.enforce_url(str(params.get("url") or ""))
        self._trace(
            run,
            "step_started",
            step=step,
            step_path=step_path,
            data={"action": step.action, "risk": decision.to_dict(), "params": _redact_params(params)},
        )

        if step.action == "control.if":
            branch_name = "then" if evaluate_condition(dict(params.get("condition") or {}), context) else "else"
            nested = [WorkflowStep.from_dict(item) for item in list(params.get(branch_name) or [])]
            await self._execute_steps(
                nested,
                run,
                context,
                cancel_event,
                path_prefix=f"{step_path}.{branch_name}",
                allow_high_risk=allow_high_risk,
                allow_critical=allow_critical,
            )
            result = {"ok": True, "branch": branch_name, "steps": len(nested)}
            self._trace(run, "step_succeeded", step=step, step_path=step_path, data=result)
            return result
        if step.action == "control.foreach":
            items = params.get("items")
            if not isinstance(items, list):
                raise WorkflowExecutionError("invalid_items", "foreach items must resolve to an array")
            max_iterations = int(params.get("max_iterations", 20))
            if len(items) > max_iterations:
                raise WorkflowExecutionError("iteration_limit", f"foreach received {len(items)} items; limit is {max_iterations}")
            item_name = str(params.get("item") or "item")
            nested_raw = list(params.get("steps") or [])
            previous = context.get(item_name, _MISSING)
            for item_index, item in enumerate(items):
                if cancel_event.is_set():
                    raise WorkflowCancelled()
                context[item_name] = item
                context[f"{item_name}_index"] = item_index
                nested = [WorkflowStep.from_dict(value) for value in nested_raw]
                await self._execute_steps(
                    nested,
                    run,
                    context,
                    cancel_event,
                    path_prefix=f"{step_path}.iterations[{item_index}]",
                    allow_high_risk=allow_high_risk,
                    allow_critical=allow_critical,
                )
            if previous is _MISSING:
                context.pop(item_name, None)
            else:
                context[item_name] = previous
            context.pop(f"{item_name}_index", None)
            result = {"ok": True, "iterations": len(items)}
            self._trace(run, "step_succeeded", step=step, step_path=step_path, data=result)
            return result

        last_error: WorkflowExecutionError | None = None
        for attempt in range(1, step.retry.max_attempts + 1):
            if cancel_event.is_set():
                raise WorkflowCancelled()
            try:
                result = await self._await_or_cancel(
                    self._execute_leaf(step, params, context, cancel_event),
                    cancel_event,
                    timeout=step.timeout_ms / 1000,
                )
                if not isinstance(result, dict):
                    result = {"ok": True, "result": result}
                if result.get("ok") is False:
                    code = str(result.get("error_type") or result.get("error") or "operation_failed")
                    raise WorkflowExecutionError(code, str(result.get("message") or code), result=result)
                self._trace(run, "step_succeeded", step=step, step_path=step_path, attempt=attempt, data=_bounded_value(result))
                return result
            except asyncio.TimeoutError:
                last_error = WorkflowExecutionError("timeout", f"Step timed out after {step.timeout_ms} ms")
            except WorkflowExecutionError as exc:
                last_error = exc
            if attempt >= step.retry.max_attempts or last_error.code not in step.retry.retry_on:
                break
            self._trace(
                run,
                "step_retry",
                step=step,
                step_path=step_path,
                attempt=attempt,
                data={"error_type": last_error.code, "backoff_ms": step.retry.backoff_ms},
            )
            await asyncio.sleep(step.retry.backoff_ms / 1000)
        assert last_error is not None
        self._trace(
            run,
            "step_failed",
            step=step,
            step_path=step_path,
            attempt=step.retry.max_attempts,
            data={"error_type": last_error.code, "message": str(last_error)},
        )
        raise last_error

    async def _execute_leaf(
        self,
        step: WorkflowStep,
        params: dict[str, Any],
        context: dict[str, Any],
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        if step.action == "data.set":
            name = str(params.get("name") or "")
            context[name] = params.get("value")
            return {"ok": True, "name": name, "value": params.get("value")}
        if step.action == "browser.wait":
            await self._cancel_aware_sleep(int(params.get("duration_ms", 500)) / 1000, cancel_event)
            return {"ok": True, "waited_ms": int(params.get("duration_ms", 500))}
        if step.action == "browser.assert":
            if bool(params.get("observe", True)):
                context["page"] = await self.runtime.execute("browser.read", params)
            if not evaluate_condition(dict(params.get("condition") or {}), context):
                raise WorkflowExecutionError("assertion_failed", str(params.get("message") or "Browser assertion failed"))
            return {"ok": True, "asserted": True}
        if step.action == "browser.wait_for":
            interval = int(params.get("interval_ms", 500)) / 1000
            while True:
                if cancel_event.is_set():
                    raise WorkflowCancelled()
                context["page"] = await self.runtime.execute("browser.read", params)
                if evaluate_condition(dict(params.get("condition") or {}), context):
                    return {"ok": True, "matched": True, "page": _bounded_value(context["page"])}
                await self._cancel_aware_sleep(interval, cancel_event)
        return await self.runtime.execute(step.action, params)

    async def _cancel_aware_sleep(self, seconds: float, cancel_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            return
        raise WorkflowCancelled()

    async def _await_or_cancel(self, awaitable: Any, cancel_event: asyncio.Event, *, timeout: float) -> Any:
        action_task = asyncio.ensure_future(awaitable)
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, _ = await asyncio.wait(
            {action_task, cancel_task},
            timeout=max(0.0, timeout),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and cancel_task.result():
            action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
            raise WorkflowCancelled()
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        if action_task not in done:
            action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
            raise asyncio.TimeoutError()
        return await action_task

    def _build_context(self, workflow: WorkflowDefinition, inputs: dict[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for name, spec in workflow.inputs.items():
            if name in inputs:
                value = inputs[name]
            elif spec.default is not None:
                value = spec.default
            elif spec.required:
                raise WorkflowExecutionError("missing_input", f"Required input is missing: {name}")
            else:
                value = None
            if value is not None and not _matches_type(value, spec.type):
                raise WorkflowExecutionError("invalid_input", f"Input {name} must be {spec.type}")
            resolved[name] = value
        unknown = sorted(set(inputs) - set(workflow.inputs))
        if unknown:
            raise WorkflowExecutionError("unknown_input", f"Unknown inputs: {', '.join(unknown)}")
        return {**resolved, "inputs": resolved, "steps": {}}

    def _preflight(
        self,
        steps: tuple[WorkflowStep, ...] | list[WorkflowStep],
        context: dict[str, Any],
        allow_high_risk: bool,
        allow_critical: bool,
    ) -> list[dict[str, Any]]:
        blocked: list[dict[str, Any]] = []
        for step in steps:
            try:
                params = _resolve_step_params(step, context)
            except TemplateResolutionError:
                params = step.params
            if not isinstance(params, dict):
                continue
            decision = self.policy.assess(step, params)
            try:
                self.policy.authorize(decision, allow_high_risk=allow_high_risk, allow_critical=allow_critical)
            except PolicyViolation as exc:
                blocked.append({"step_id": step.id, "action": step.action, "code": exc.code, "risk": decision.to_dict()})
            if step.action == "control.if":
                for branch in ("then", "else"):
                    nested = [WorkflowStep.from_dict(item) for item in list(params.get(branch) or [])]
                    blocked.extend(self._preflight(nested, context, allow_high_risk, allow_critical))
            elif step.action == "control.foreach":
                nested = [WorkflowStep.from_dict(item) for item in list(params.get("steps") or [])]
                blocked.extend(self._preflight(nested, context, allow_high_risk, allow_critical))
        return blocked

    def _plan(self, steps: tuple[WorkflowStep, ...] | list[WorkflowStep], context: dict[str, Any]) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        for step in steps:
            try:
                params = _resolve_step_params(step, context)
            except TemplateResolutionError:
                params = step.params
            decision = self.policy.assess(step, params if isinstance(params, dict) else step.params)
            plan.append({"id": step.id, "action": step.action, "risk": decision.to_dict(), "params": _redact_params(params)})
        return plan

    def _checkpoint(self, run: RunRecord) -> None:
        run.updated_at = utc_now()
        self.store.save_run(run)

    def _trace(
        self,
        run: RunRecord,
        event: str,
        *,
        step: WorkflowStep | None = None,
        step_path: str = "",
        attempt: int = 0,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_trace(
            TraceEvent(
                run_id=run.id,
                event=event,
                step_id=step.id if step else "",
                step_path=step_path,
                attempt=attempt,
                data=_bounded_value(_scrub_secrets(data or {}, self._secret_values.get(run.id, set()))),
            )
        )


_MISSING = object()


def _resolve_step_params(step: WorkflowStep, context: dict[str, Any]) -> dict[str, Any]:
    """Resolve a control step's envelope without resolving its future child scope."""
    if step.action in {"browser.wait_for", "browser.assert"}:
        result = {
            key: resolve_value(value, context)
            for key, value in step.params.items()
            if key != "condition"
        }
        result["condition"] = step.params.get("condition")
        return result
    if step.action == "control.if":
        result = dict(step.params)
        result["condition"] = resolve_value(step.params.get("condition"), context)
        return result
    if step.action == "control.foreach":
        result = dict(step.params)
        result["items"] = resolve_value(step.params.get("items"), context)
        return result
    resolved = resolve_value(step.params, context)
    if not isinstance(resolved, dict):
        raise TemplateResolutionError("Resolved step params must be an object")
    return resolved


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def _redact_params(params: Any) -> Any:
    if not isinstance(params, dict):
        return _bounded_value(params)
    result: dict[str, Any] = {}
    for key, value in params.items():
        lowered = str(key).lower()
        if any(word in lowered for word in ("password", "secret", "token", "api_key", "otp", "cvv")):
            result[key] = "[REDACTED]"
        else:
            result[key] = _bounded_value(value)
    return result


def _bounded_mapping(value: dict[str, Any]) -> dict[str, Any]:
    bounded = _bounded_value(value)
    return bounded if isinstance(bounded, dict) else {"truncated": True}


def _bounded_value(value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:4000]
    if len(encoded.encode("utf-8")) <= 64 * 1024:
        try:
            return json.loads(encoded)
        except ValueError:
            return str(value)[:4000]
    if isinstance(value, str):
        return value[:8000] + "...[truncated]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            result[str(key)] = _bounded_value(item)
            if len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")) > 48 * 1024:
                result["truncated"] = True
                break
        return result
    if isinstance(value, list):
        return [_bounded_value(item) for item in value[:50]] + (["...[truncated]"] if len(value) > 50 else [])
    return str(value)[:8000]


def _scrub_secrets(value: Any, secrets: set[str]) -> Any:
    usable = {secret for secret in secrets if len(secret) >= 3}
    if not usable:
        return value
    if isinstance(value, dict):
        return {key: _scrub_secrets(item, usable) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_secrets(item, usable) for item in value]
    if isinstance(value, str):
        result = value
        for secret in usable:
            result = result.replace(secret, "[REDACTED]")
        return result
    return value
