from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_logs_and_cli_are_separate_workspace_entries():
    workspace_js = (ROOT / "pawmate/ui/web/js/workspace.js").read_text(encoding="utf-8")
    app_js = (ROOT / "pawmate/ui/web/js/app.js").read_text(encoding="utf-8")
    view_py = (ROOT / "pawmate/ui/web_chat_view.py").read_text(encoding="utf-8")

    assert '["logs", "日志"' in workspace_js
    assert '["cli", "CLI"' in workspace_js
    assert 'logs: ["日志", "PawMate 实时运行日志"]' in workspace_js
    assert 'cli: ["Agent CLI", "PawMate 对话与显式 PowerShell"]' in workspace_js
    assert "function renderLogs()" in workspace_js
    assert "function renderCli()" in workspace_js
    assert "PawMate Agent CLI" in workspace_js
    assert 'command.indexOf("/shell ") === 0' in workspace_js
    assert 'cliCall("submitAgent", [command]' in workspace_js
    assert 'cliCall("getAgentStatus", [], applyCliAgentStatus)' in workspace_js
    assert 'cliCall("start", [], applyCliStatus)' not in workspace_js
    assert "renderTerminal" not in workspace_js
    assert "window.cliBridge = channel.objects.cliBridge" in app_js
    assert 'registerObject("cliBridge"' in view_py


def test_browser_settings_button_stays_inside_browser_workbench():
    workspace_js = (ROOT / "pawmate/ui/web/js/workspace.js").read_text(encoding="utf-8")

    assert 'browserSurface = "workbench"' in workspace_js
    assert 'browserTab = "settings"' in workspace_js
    assert 'window.PawSettings.openSection("browser")' not in workspace_js
    assert "function renderBrowserWorkbench()" in workspace_js
