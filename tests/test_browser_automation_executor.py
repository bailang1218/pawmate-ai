import json

import pytest

from pawmate.browser_automation.executor import WorkflowExecutor
from pawmate.browser_automation.models import RunRecord, RunStatus, WorkflowDefinition
from pawmate.browser_automation.store import AutomationStore
from pawmate.browser_automation.validation import WorkflowValidator


class FakeRuntime:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    async def execute(self, action, params):
        self.calls.append((action, params))
        if self.results:
            return self.results.pop(0)
        return {"ok": True, "action": action, "url": params.get("url", "https://example.test")}


def _definition(value):
    report = WorkflowValidator().validate(value)
    assert report.valid, report.to_dict()
    value = {"id": "wf_test", **value}
    return WorkflowDefinition.from_dict(value)


@pytest.mark.asyncio
async def test_executor_runs_foreach_without_resolving_future_item_scope(tmp_path):
    value = {
        "name": "loop",
        "version": 1,
        "inputs": {"items": {"type": "array", "required": True}},
        "steps": [
            {
                "id": "loop",
                "action": "control.foreach",
                "params": {
                    "items": "{{inputs.items}}",
                    "item": "row",
                    "max_iterations": 5,
                    "steps": [
                        {"id": "remember", "action": "data.set", "params": {"name": "last", "value": "{{row}}"}}
                    ],
                },
            }
        ],
    }
    workflow = _definition(value)
    store = AutomationStore(tmp_path)
    runtime = FakeRuntime()
    run = RunRecord(id="run_loop", workflow_id=workflow.id, workflow_version=1)

    result = await WorkflowExecutor(store, runtime).execute(run, workflow, {"items": ["a", "b"]})

    assert result.status == RunStatus.SUCCEEDED
    assert result.outputs["remember"]["value"] == "b"
    assert len(result.completed_steps) == 3


@pytest.mark.asyncio
async def test_executor_stops_before_critical_action_without_approval(tmp_path):
    workflow = _definition(
        {
            "name": "payment",
            "version": 1,
            "steps": [{"id": "pay", "action": "browser.click", "params": {"intent": "pay now"}}],
        }
    )
    store = AutomationStore(tmp_path)
    runtime = FakeRuntime()
    run = RunRecord(id="run_pay", workflow_id=workflow.id, workflow_version=1)

    result = await WorkflowExecutor(store, runtime).execute(run, workflow, {})

    assert result.status == RunStatus.APPROVAL_REQUIRED
    assert result.approval["steps"][0]["code"] == "critical_approval_required"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_dry_run_reports_critical_risk_without_requiring_approval(tmp_path):
    workflow = _definition(
        {
            "name": "payment plan",
            "version": 1,
            "steps": [{"id": "pay", "action": "browser.click", "params": {"intent": "pay now"}}],
        }
    )
    store = AutomationStore(tmp_path)
    run = RunRecord(id="run_pay_plan", workflow_id=workflow.id, workflow_version=1, dry_run=True)

    result = await WorkflowExecutor(store, FakeRuntime()).execute(run, workflow, {})

    assert result.status == RunStatus.SUCCEEDED
    assert result.outputs["plan"][0]["risk"]["critical"] is True


@pytest.mark.asyncio
async def test_executor_retries_only_safe_read_and_checkpoints_trace(tmp_path):
    workflow = _definition(
        {
            "name": "retry read",
            "version": 1,
            "steps": [
                {
                    "id": "read",
                    "action": "browser.read",
                    "params": {},
                    "retry": {"max_attempts": 2, "backoff_ms": 0, "retry_on": ["selector_not_found"]},
                }
            ],
        }
    )
    store = AutomationStore(tmp_path)
    runtime = FakeRuntime(
        [
            {"ok": False, "error_type": "selector_not_found", "message": "stale"},
            {"ok": True, "elements": []},
        ]
    )
    run = RunRecord(id="run_retry", workflow_id=workflow.id, workflow_version=1)

    result = await WorkflowExecutor(store, runtime).execute(run, workflow, {})

    assert result.status == RunStatus.SUCCEEDED
    assert len(runtime.calls) == 2
    assert any(event["event"] == "step_retry" for event in store.read_trace(run.id))


@pytest.mark.asyncio
async def test_wait_for_condition_observes_page_before_resolving_page_variable(tmp_path):
    workflow = _definition(
        {
            "name": "wait page",
            "version": 1,
            "steps": [
                {
                    "id": "ready",
                    "action": "browser.wait_for",
                    "timeout_ms": 2000,
                    "params": {
                        "condition": {"left": "{{page.title}}", "op": "eq", "right": "Ready"},
                        "interval_ms": 100,
                    },
                }
            ],
        }
    )
    store = AutomationStore(tmp_path)
    runtime = FakeRuntime([{"ok": True, "title": "Loading"}, {"ok": True, "title": "Ready"}])
    run = RunRecord(id="run_wait", workflow_id=workflow.id, workflow_version=1)

    result = await WorkflowExecutor(store, runtime).execute(run, workflow, {})

    assert result.status == RunStatus.SUCCEEDED
    assert len(runtime.calls) == 2


@pytest.mark.asyncio
async def test_executor_never_persists_secret_input_value(tmp_path):
    workflow = _definition(
        {
            "name": "secret",
            "version": 1,
            "inputs": {"password": {"type": "string", "required": True, "secret": True}},
            "steps": [
                {
                    "id": "remember",
                    "action": "data.set",
                    "params": {"name": "temporary", "value": "{{inputs.password}}"},
                }
            ],
        }
    )
    store = AutomationStore(tmp_path)
    run = RunRecord(id="run_secret", workflow_id=workflow.id, workflow_version=1, inputs={"password": "[REDACTED]"})

    result = await WorkflowExecutor(store, FakeRuntime()).execute(run, workflow, {"password": "super-secret-value"})

    persisted = json.dumps(store.get_run(run.id).to_dict(), ensure_ascii=False)
    trace = json.dumps(store.read_trace(run.id), ensure_ascii=False)
    assert result.status == RunStatus.SUCCEEDED
    assert "super-secret-value" not in persisted
    assert "super-secret-value" not in trace
