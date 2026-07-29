from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future

from pawmate.bridge.browser_automation_bridge import BrowserAutomationBridge


class _Signal:
    def connect(self, _callback):
        return None


class _Worker:
    ready = _Signal()

    def is_ready(self) -> bool:
        return True

    def submit_coroutine(self, coroutine):
        future = Future()
        try:
            future.set_result(asyncio.run(coroutine))
        except Exception as exc:
            future.set_exception(exc)
        return future


class _Service:
    busy = False
    active_run_id = ""

    def __init__(self):
        self.closed = False
        self.received = None

    def list_workflows(self):
        return []

    def list_runs(self, *, limit=100):
        self.received = {"limit": limit}
        return []

    async def start_agent_run(self, goal, **options):
        self.received = {"goal": goal, **options}
        return {"id": "run_test", "status": "queued"}

    async def close(self):
        self.closed = True


def test_bridge_dispatches_async_work_to_worker_and_emits_result():
    service = _Service()
    bridge = BrowserAutomationBridge(service=service)
    completed = []
    bridge.operationFinished.connect(lambda request_id, raw: completed.append((request_id, json.loads(raw))))
    bridge.set_worker(_Worker())

    accepted = json.loads(
        bridge.startAgentRun(
            json.dumps({"goal": "read the page", "allow_model_data": True})
        )
    )

    assert accepted["ok"] is True
    assert accepted["accepted"] is True
    assert completed[0][0] == accepted["request_id"]
    assert completed[0][1]["result"]["id"] == "run_test"
    assert service.received["goal"] == "read the page"


def test_bridge_rejects_invalid_payload_before_dispatch():
    bridge = BrowserAutomationBridge(service=_Service())
    bridge.set_worker(_Worker())

    result = json.loads(bridge.startAgentRun("[]"))

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


def test_bridge_close_uses_worker_loop_and_marks_service_unavailable():
    service = _Service()
    bridge = BrowserAutomationBridge(service=service)
    bridge.set_worker(_Worker())

    bridge.close()
    status = json.loads(bridge.getStatus())

    assert service.closed is True
    assert status["ready"] is False


def test_bridge_lists_recent_runs():
    service = _Service()
    bridge = BrowserAutomationBridge(service=service)

    result = json.loads(bridge.listRuns("25"))

    assert result == {"ok": True, "runs": [], "count": 0}
    assert service.received == {"limit": 25}
