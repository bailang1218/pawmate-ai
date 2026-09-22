import asyncio

import pytest

from pawmate.browser_automation.service import BrowserAutomationService, ConflictError, ValidationError
from pawmate.browser_automation.models import RunRecord, RunStatus
from pawmate.browser_automation.store import AutomationStore


class BlockingRuntime:
    def __init__(self):
        self.started = asyncio.Event()

    async def execute(self, action, params):
        self.started.set()
        await asyncio.Event().wait()


class FastRuntime:
    async def execute(self, action, params):
        return {"ok": True, "action": action, "elements": []}


def _workflow(name="test"):
    return {
        "name": name,
        "version": 1,
        "steps": [{"id": "read", "action": "browser.read", "params": {}}],
    }


@pytest.mark.asyncio
async def test_service_cancellation_is_registered_before_task_starts(tmp_path):
    runtime = BlockingRuntime()
    service = BrowserAutomationService(store=AutomationStore(tmp_path), runtime=runtime)
    workflow = service.create_workflow(_workflow())

    run = await service.start_workflow_run(workflow.id)
    assert service.cancel_run(run.id) is True
    finished = await service.wait_run(run.id)

    assert finished.status.value == "cancelled"


@pytest.mark.asyncio
async def test_cross_process_style_lease_blocks_second_service_run(tmp_path):
    first_runtime = BlockingRuntime()
    first = BrowserAutomationService(store=AutomationStore(tmp_path), runtime=first_runtime)
    second = BrowserAutomationService(store=AutomationStore(tmp_path), runtime=FastRuntime())
    workflow = first.create_workflow(_workflow())
    run = await first.start_workflow_run(workflow.id)

    with pytest.raises(ConflictError, match="another PawMate process"):
        await second.start_workflow_run(workflow.id)

    first.cancel_run(run.id)
    await first.wait_run(run.id)
    await asyncio.sleep(0)
    second_run = await second.start_workflow_run(workflow.id, dry_run=True)
    assert (await second.wait_run(second_run.id)).status.value == "succeeded"


def test_service_preserves_workflow_versions(tmp_path):
    service = BrowserAutomationService(store=AutomationStore(tmp_path), runtime=FastRuntime())
    first = service.create_workflow(_workflow("version one"))
    second = service.update_workflow(
        first.id,
        {
            "name": "version two",
            "steps": [{"id": "read", "action": "browser.read", "params": {}}],
        },
    )

    assert second.version == 2
    assert service.get_workflow_version(first.id, 1).name == "version one"
    assert service.get_workflow_version(first.id, 2).name == "version two"


@pytest.mark.asyncio
async def test_service_requires_explicit_model_data_consent(tmp_path):
    service = BrowserAutomationService(store=AutomationStore(tmp_path), runtime=FastRuntime())

    with pytest.raises(ValidationError, match="allow_model_data"):
        await service.start_agent_run("open example.test")


@pytest.mark.asyncio
async def test_agent_options_are_validated_before_run_is_persisted(tmp_path):
    store = AutomationStore(tmp_path)
    service = BrowserAutomationService(store=store, runtime=FastRuntime())

    with pytest.raises(ValidationError, match="max_turns"):
        await service.start_agent_run(
            "private goal",
            max_turns=0,
            allow_model_data=True,
        )

    assert store.list_runs() == []

    run = await service.start_agent_run(
        "goal containing private data",
        max_turns=1,
        allow_model_data=True,
    )
    assert run.inputs == {"goal": "[REDACTED]"}
    assert service.cancel_run(run.id) is True
    await service.wait_run(run.id)


def test_service_marks_stale_running_checkpoint_interrupted_without_replay(tmp_path):
    store = AutomationStore(tmp_path)
    stale = RunRecord(
        id="run_stale",
        workflow_id="wf_stale",
        workflow_version=1,
        status=RunStatus.RUNNING,
        current_step="steps[2]",
        completed_steps=["steps[0]", "steps[1]"],
    )
    store.save_run(stale)

    BrowserAutomationService(store=store, runtime=FastRuntime())
    recovered = store.get_run(stale.id)

    assert recovered.status == RunStatus.FAILED
    assert recovered.error["code"] == "process_interrupted"
    assert recovered.completed_steps == ["steps[0]", "steps[1]"]
    assert any(event["event"] == "run_recovered_as_interrupted" for event in store.read_trace(stale.id))


def test_service_lists_recent_runs_in_newest_first_order(tmp_path):
    store = AutomationStore(tmp_path)
    service = BrowserAutomationService(store=store, runtime=FastRuntime())
    store.save_run(RunRecord(id="run_old", workflow_id="wf", workflow_version=1, created_at="2026-01-01T00:00:00Z"))
    store.save_run(RunRecord(id="run_new", workflow_id="wf", workflow_version=1, created_at="2026-01-02T00:00:00Z"))

    assert [run.id for run in service.list_runs(limit=1)] == ["run_new"]
    with pytest.raises(ValidationError, match="limit must be 1..500"):
        service.list_runs(limit=0)
