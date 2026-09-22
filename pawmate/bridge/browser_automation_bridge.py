"""QWebChannel facade for PawMate's browser automation service."""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from concurrent.futures import Future
from typing import Any, Callable

from pawmate.browser_automation.models import RunRecord, WorkflowDefinition
from pawmate.browser_automation.service import AutomationServiceError, BrowserAutomationService
from pawmate.browser_automation.validation import (
    ALLOWED_ACTIONS,
    MAX_ITERATIONS,
    MAX_NESTING,
    MAX_STEPS,
    MAX_TIMEOUT_MS,
    MAX_WORKFLOW_BYTES,
)
from pawmate.qt_compat import QObject, Signal, Slot


logger = logging.getLogger("pawmate.browser_automation.bridge")
API_VERSION = "1.0"
MAX_BODY_BYTES = 256 * 1024


class BrowserAutomationBridge(QObject):
    """Expose the automation service without blocking Qt's GUI thread.

    Async and mutating calls immediately return a request id. Their final JSON
    payload is delivered through ``operationFinished``.
    """

    operationFinished = Signal(str, str)
    stateChanged = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        service: BrowserAutomationService | None = None,
    ):
        super().__init__(parent)
        self._worker: Any = None
        self._service: BrowserAutomationService | None = service or BrowserAutomationService()
        self._closing = False

    def set_worker(self, worker: Any) -> None:
        if worker is self._worker and self._service is not None:
            return
        if self._worker is not None:
            raise RuntimeError("Browser automation worker must be closed before replacement")
        if self._service is None:
            self._service = BrowserAutomationService()
        self._worker = worker
        self._closing = False
        ready_signal = getattr(worker, "ready", None)
        if ready_signal is not None:
            ready_signal.connect(self._emit_state)
        self._emit_state()

    def close(self, *, timeout: float = 7.0) -> None:
        """Stop an active browser run before its owning worker loop exits."""
        service = self._service
        worker = self._worker
        self._closing = True
        if service is not None and worker is not None and worker.is_ready():
            try:
                worker.submit_coroutine(service.close()).result(timeout=timeout)
            except Exception as exc:
                logger.warning("Browser automation shutdown did not finish cleanly: %s", exc)
        self._worker = None
        self._service = None
        self._emit_state()

    @Slot(result=str)
    def getStatus(self) -> str:
        service = self._service
        ready = bool(self._worker is not None and self._worker.is_ready() and not self._closing)
        return _json(
            {
                "ok": True,
                "service": "pawmate-browser-automation",
                "version": API_VERSION,
                "ready": ready,
                "busy": bool(service and service.busy),
                "active_run_id": service.active_run_id if service else "",
                "modes": ["agent", "workflow", "ai_generated_workflow"],
                "browser_surface": "native_workbench",
            }
        )

    @Slot(result=str)
    def getCapabilities(self) -> str:
        return _json(
            {
                "ok": True,
                "api_version": API_VERSION,
                "actions": sorted(ALLOWED_ACTIONS),
                "limits": {
                    "workflow_bytes": MAX_WORKFLOW_BYTES,
                    "steps": MAX_STEPS,
                    "nesting": MAX_NESTING,
                    "iterations": MAX_ITERATIONS,
                    "step_timeout_ms": MAX_TIMEOUT_MS,
                    "request_body_bytes": MAX_BODY_BYTES,
                },
                "risk_defaults": {
                    "high_requires_approval": True,
                    "critical_requires_scoped_approval": True,
                    "model_data_requires_consent": True,
                },
            }
        )

    @Slot(str, result=str)
    def validateWorkflow(self, raw: str) -> str:
        try:
            return _json({"ok": True, **self._require_service().validate_workflow(_payload(raw))})
        except Exception as exc:
            return _error(exc)

    @Slot(result=str)
    def listWorkflows(self) -> str:
        try:
            workflows = [item.to_dict() for item in self._require_service().list_workflows()]
            return _json({"ok": True, "workflows": workflows, "count": len(workflows)})
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def getWorkflow(self, workflow_id: str) -> str:
        try:
            workflow = self._require_service().get_workflow(workflow_id)
            return _json({"ok": True, "workflow": workflow.to_dict()})
        except Exception as exc:
            return _error(exc)

    @Slot(str, str, result=str)
    def getWorkflowVersion(self, workflow_id: str, version: str) -> str:
        try:
            workflow = self._require_service().get_workflow_version(workflow_id, int(version))
            return _json({"ok": True, "workflow": workflow.to_dict()})
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def createWorkflow(self, raw: str) -> str:
        try:
            payload = _payload(raw)
            return self._submit("create_workflow", lambda service: service.create_workflow(payload))
        except Exception as exc:
            return _error(exc)

    @Slot(str, str, result=str)
    def updateWorkflow(self, workflow_id: str, raw: str) -> str:
        try:
            payload = _payload(raw)
            return self._submit(
                "update_workflow",
                lambda service: service.update_workflow(workflow_id, payload),
            )
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def generateWorkflow(self, raw: str) -> str:
        try:
            payload = _payload(raw)
            goal = str(payload.get("goal") or "")
            context = payload.get("context") or {}
            if not isinstance(context, dict):
                raise ValueError("context must be an object")
            return self._submit(
                "generate_workflow",
                lambda service: service.generate_workflow(
                    goal,
                    context,
                    save=payload.get("save") is True,
                    allow_model_data=payload.get("allow_model_data") is True,
                ),
            )
        except Exception as exc:
            return _error(exc)

    @Slot(str, str, result=str)
    def startWorkflowRun(self, workflow_id: str, raw: str) -> str:
        try:
            payload = _payload(raw, empty=True)
            inputs = payload.get("inputs") or {}
            if not isinstance(inputs, dict):
                raise ValueError("inputs must be an object")
            return self._submit(
                "start_workflow_run",
                lambda service: service.start_workflow_run(
                    workflow_id,
                    inputs=inputs,
                    dry_run=payload.get("dry_run") is True,
                    allow_high_risk=payload.get("allow_high_risk") is True,
                    allow_critical=payload.get("allow_critical") is True,
                    approval=payload.get("approval"),
                ),
            )
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def startAgentRun(self, raw: str) -> str:
        try:
            payload = _payload(raw)
            return self._submit(
                "start_agent_run",
                lambda service: service.start_agent_run(
                    str(payload.get("goal") or ""),
                    max_turns=int(payload.get("max_turns", 20)),
                    step_timeout_ms=int(payload.get("step_timeout_ms", 30_000)),
                    allow_high_risk=payload.get("allow_high_risk") is True,
                    allow_critical=payload.get("allow_critical") is True,
                    allow_model_data=payload.get("allow_model_data") is True,
                    visibility=str(payload.get("visibility") or "auto"),
                    approval=payload.get("approval"),
                ),
            )
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def getRun(self, run_id: str) -> str:
        try:
            run = self._require_service().get_run(run_id)
            return _json({"ok": True, "run": run.to_dict()})
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def listRuns(self, limit_text: str = "100") -> str:
        try:
            runs = [item.to_dict() for item in self._require_service().list_runs(limit=int(limit_text or "100"))]
            return _json({"ok": True, "runs": runs, "count": len(runs)})
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def getTrace(self, run_id: str) -> str:
        try:
            trace = self._require_service().get_trace(run_id)
            return _json({"ok": True, "trace": trace, "count": len(trace)})
        except Exception as exc:
            return _error(exc)

    @Slot(str, result=str)
    def cancelRun(self, run_id: str) -> str:
        try:
            return self._submit("cancel_run", lambda service: service.cancel_run(run_id))
        except Exception as exc:
            return _error(exc)

    def _require_service(self) -> BrowserAutomationService:
        if self._service is None:
            raise RuntimeError("Browser automation service is restarting")
        return self._service

    def _submit(
        self,
        operation: str,
        callback: Callable[[BrowserAutomationService], Any],
    ) -> str:
        service = self._require_service()
        worker = self._worker
        if self._closing or worker is None or not worker.is_ready():
            raise RuntimeError("Browser automation worker is not ready")
        request_id = f"req_{uuid.uuid4().hex[:20]}"

        async def invoke() -> Any:
            result = callback(service)
            if inspect.isawaitable(result):
                result = await result
            return result

        future = worker.submit_coroutine(invoke())
        future.add_done_callback(
            lambda completed, rid=request_id, op=operation: self._operation_done(rid, op, completed)
        )
        return _json({"ok": True, "accepted": True, "request_id": request_id})

    def _operation_done(self, request_id: str, operation: str, future: Future) -> None:
        try:
            payload = {
                "ok": True,
                "request_id": request_id,
                "operation": operation,
                "result": _serializable(future.result()),
            }
        except Exception as exc:
            payload = json.loads(_error(exc))
            payload.update(request_id=request_id, operation=operation)
        self.operationFinished.emit(request_id, _json(payload))
        self._emit_state()

    def _emit_state(self) -> None:
        try:
            self.stateChanged.emit(self.getStatus())
        except RuntimeError:
            pass


def _payload(raw: str, *, empty: bool = False) -> dict[str, Any]:
    encoded = str(raw or "").encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        raise ValueError(f"Request exceeds {MAX_BODY_BYTES} bytes")
    if not encoded and empty:
        return {}
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request must be a valid JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("Request must be a JSON object")
    return value


def _serializable(value: Any) -> Any:
    if isinstance(value, (RunRecord, WorkflowDefinition)):
        return value.to_dict()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return value


def _error(exc: Exception) -> str:
    if isinstance(exc, AutomationServiceError):
        error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.details is not None:
            error["details"] = exc.details
    elif isinstance(exc, (TypeError, ValueError)):
        error = {"code": "invalid_request", "message": str(exc)}
    elif isinstance(exc, RuntimeError):
        error = {"code": "runtime_unavailable", "message": str(exc)}
    else:
        logger.exception("Browser automation bridge request failed", exc_info=exc)
        error = {"code": "internal_error", "message": "Internal browser automation error"}
    return _json({"ok": False, "error": error})


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
