from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_browser_settings_have_one_workspace_entry_and_one_launch_toggle():
    index_html = (ROOT / "pawmate/ui/web/index.html").read_text(encoding="utf-8")
    settings_js = (ROOT / "pawmate/ui/web/js/settings/settings.js").read_text(encoding="utf-8")
    workspace_js = (ROOT / "pawmate/ui/web/js/workspace.js").read_text(encoding="utf-8")

    assert 'data-section="browser"' not in index_html
    assert '"browser"' not in settings_js.split("const VALID_SECTIONS =", 1)[1].split(";", 1)[0]
    assert "cfgAllowBrowserProcessLaunch" not in settings_js
    assert workspace_js.count("wbAllowBrowserProcessLaunch") == 2
    assert "security.allow_browser_process_launch" in workspace_js
    assert "浏览器运行与安全设置" in workspace_js
