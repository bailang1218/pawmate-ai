import pytest
from aiohttp.test_utils import TestClient, TestServer

from pawmate.browser_automation.api import create_app
from pawmate.browser_automation.service import BrowserAutomationService
from pawmate.browser_automation.store import AutomationStore


TOKEN = "test-token-that-is-long-enough-123456"


class FakeRuntime:
    async def execute(self, action, params):
        return {"ok": True, "action": action, "url": params.get("url", "https://example.test"), "elements": []}


@pytest.fixture
async def api_client(tmp_path):
    service = BrowserAutomationService(store=AutomationStore(tmp_path), runtime=FakeRuntime())
    client = TestClient(TestServer(create_app(service, token=TOKEN)))
    await client.start_server()
    try:
        yield client, service
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_requires_token_except_for_minimal_health(api_client):
    client, _ = api_client
    health = await client.get("/health")
    denied = await client.get("/v1/workflows")

    assert health.status == 200
    assert (await health.json())["modes"] == ["agent", "workflow", "ai_generated_workflow"]
    assert denied.status == 401


@pytest.mark.asyncio
async def test_api_creates_and_dry_runs_workflow(api_client):
    client, service = api_client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    workflow = {
        "name": "api test",
        "version": 1,
        "steps": [{"id": "open", "action": "browser.goto", "params": {"url": "https://example.test"}}],
    }
    created_response = await client.post("/v1/workflows", json=workflow, headers=headers)
    assert created_response.status == 201
    workflow_id = (await created_response.json())["workflow"]["id"]

    run_response = await client.post(
        f"/v1/workflows/{workflow_id}/runs",
        json={"dry_run": True},
        headers=headers,
    )
    assert run_response.status == 202
    run_id = (await run_response.json())["run"]["id"]
    finished = await service.wait_run(run_id)

    assert finished.status.value == "succeeded"
    assert finished.outputs["plan"][0]["action"] == "browser.goto"

    listed_response = await client.get("/v1/runs?limit=10", headers=headers)
    listed = await listed_response.json()
    assert listed_response.status == 200
    assert listed["count"] == 1
    assert listed["runs"][0]["id"] == run_id


@pytest.mark.asyncio
async def test_api_returns_structured_validation_error(api_client):
    client, _ = api_client
    response = await client.post(
        "/v1/workflows",
        json={"name": "bad", "steps": [{"id": "x", "action": "browser.evaluate", "params": {}}]},
        headers={"X-PawMate-Token": TOKEN},
    )
    body = await response.json()

    assert response.status == 422
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["valid"] is False
