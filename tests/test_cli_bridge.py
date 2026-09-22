from __future__ import annotations

import json
import os
import time

from pawmate.bridge.cli_bridge import CliBridge, MAX_COMMAND_CHARS
from pawmate.core.services.chat_service import ChatService
from pawmate.qt_compat import QCoreApplication


def _wait_until(app: QCoreApplication, predicate, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def test_cli_bridge_runs_a_real_persistent_shell_command(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    bridge = CliBridge(working_directory=tmp_path)
    output: list[str] = []
    bridge.outputReady.connect(output.append)

    started = json.loads(bridge.start())
    assert started["ok"] is True
    assert _wait_until(app, lambda: json.loads(bridge.getStatus())["state"] == "running")

    accepted = json.loads(bridge.writeLine("echo pawmate-cli-smoke"))
    assert accepted["ok"] is True
    assert accepted["accepted"] is True
    assert _wait_until(app, lambda: "pawmate-cli-smoke" in "".join(output))

    if os.name == "nt":
        expected = "\u4f1a\u8bdd\u4fdd\u7559"
        bridge.writeLine(f"$pawmateCliValue = '{expected}'")
        bridge.writeLine("Write-Output $pawmateCliValue")
        assert _wait_until(app, lambda: expected in "".join(output)), repr(
            "".join(output).encode("unicode_escape")
        )
    else:
        bridge.writeLine("export PAWMATE_CLI_VALUE=persisted")
        bridge.writeLine("echo $PAWMATE_CLI_VALUE")
        assert _wait_until(app, lambda: "persisted" in "".join(output))

    if os.name == "nt":
        bridge.writeLine("Start-Sleep -Seconds 5")
    else:
        bridge.writeLine("sleep 5")
    interrupted = json.loads(bridge.interrupt())
    assert interrupted["interrupting"] is True
    start_banner = f"[CLI] {started['shell']} 已启动"
    assert _wait_until(app, lambda: "".join(output).count(start_banner) >= 2)
    bridge.writeLine("echo pawmate-cli-after-interrupt")
    assert _wait_until(app, lambda: "pawmate-cli-after-interrupt" in "".join(output))

    bridge.shutdown()
    assert _wait_until(app, lambda: json.loads(bridge.getStatus())["state"] == "stopped")


def test_cli_bridge_rejects_invalid_command_payloads(tmp_path):
    bridge = CliBridge(working_directory=tmp_path)

    assert json.loads(bridge.writeLine("bad\x00command"))["ok"] is False
    assert json.loads(bridge.writeLine("x" * (MAX_COMMAND_CHARS + 1)))["ok"] is False
    assert json.loads(bridge.getStatus())["state"] == "stopped"
    bridge.shutdown()


def test_agent_cli_streams_through_shared_chat_service_without_web_chat_leak(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    service = ChatService({"ui": {"web_ui_mock_mode": True}})
    service.start_listening()
    bridge = CliBridge(working_directory=tmp_path)
    bridge.set_chat_service(service)

    agent_output: list[str] = []
    agent_states: list[dict] = []
    web_output: list[str] = []
    web_finished: list[int] = []
    bridge.agentOutputReady.connect(agent_output.append)
    bridge.agentStateChanged.connect(lambda raw: agent_states.append(json.loads(raw)))
    service.text_delta_turn.connect(lambda _turn, text: web_output.append(text))
    service.finished_turn.connect(web_finished.append)

    accepted = json.loads(bridge.submitAgent("你好"))
    assert accepted["ok"] is True
    assert accepted["accepted"] is True
    assert accepted["turnId"] > 0
    assert json.loads(bridge.getStatus())["state"] == "stopped"

    assert _wait_until(
        app,
        lambda: json.loads(bridge.getAgentStatus())["state"] == "ready",
    )
    joined_output = "".join(agent_output)
    assert "[Mock] Received:" in joined_output
    assert "你好" in joined_output
    assert web_output == []
    assert web_finished == []
    assert any(state["state"] == "answering" for state in agent_states)
    bridge.shutdown()


def test_agent_cli_cancel_targets_only_its_agent_turn(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    service = ChatService({"ui": {"web_ui_mock_mode": True}})
    service.start_listening()
    bridge = CliBridge(working_directory=tmp_path)
    bridge.set_chat_service(service)
    output: list[str] = []
    bridge.agentOutputReady.connect(output.append)

    accepted = json.loads(bridge.submitAgent("执行一个需要取消的任务"))
    turn_id = accepted["turnId"]
    cancelled = json.loads(bridge.cancelAgent())

    assert cancelled["cancelled"] is True
    assert service.is_turn_active(turn_id) is False
    assert _wait_until(
        app,
        lambda: json.loads(bridge.getAgentStatus())["state"] == "ready",
    )
    assert "Task cancelled" in "".join(output)
    bridge.shutdown()
