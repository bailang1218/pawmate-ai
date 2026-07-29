from pawmate.browser_automation.ai import (
    AgentAutomationRunner,
    AgentRunOptions,
    SchemaWorkflowGenerator,
    _parse_json_object,
)
from pawmate.browser_automation.models import RunRecord
from pawmate.browser_automation.store import AutomationStore


class FakeJsonModel:
    async def complete_json(self, *, system, prompt, max_tokens):
        return {
            "name": "generated",
            "version": 1,
            "steps": [{"id": "read", "action": "browser.read", "params": {}}],
        }


class FakePlanner:
    def __init__(self):
        self.turn = 0

    async def next_action(self, goal, observation, history):
        self.turn += 1
        if self.turn == 1:
            return {
                "done": False,
                "action": "browser.goto",
                "params": {"url": "https://example.test"},
                "reason": "open target",
            }
        return {"done": True, "summary": "opened"}


class FakeRuntime:
    def __init__(self):
        self.calls = []

    async def execute(self, action, params):
        self.calls.append((action, params))
        return {"ok": True, "action": action, "elements": [], "url": params.get("url", "https://example.test")}


async def test_schema_generator_uses_injected_model():
    workflow = await SchemaWorkflowGenerator(FakeJsonModel()).generate("read the current page", {})

    assert workflow["steps"][0]["action"] == "browser.read"


async def test_agent_runner_observes_plans_and_finishes(tmp_path):
    store = AutomationStore(tmp_path)
    runtime = FakeRuntime()
    runner = AgentAutomationRunner(store, runtime, FakePlanner())
    run = RunRecord(id="run_agent", workflow_id="agent_direct", workflow_version=1, mode="agent")

    result = await runner.run(run, "open example", AgentRunOptions(max_turns=3))

    assert result.status.value == "succeeded"
    assert result.outputs["summary"] == "opened"
    assert [call[0] for call in runtime.calls] == ["browser.read", "browser.goto", "browser.read"]


def test_model_json_parser_rejects_trailing_prose():
    try:
        _parse_json_object('{"ok": true} trailing')
    except ValueError as exc:
        assert "trailing" in str(exc)
    else:
        raise AssertionError("trailing prose should be rejected")
