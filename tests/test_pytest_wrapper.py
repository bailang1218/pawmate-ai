from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from pawmate.tools.browser.preflight import RawProc, infer_native_state, needs_graceful_restart


ROOT = Path(__file__).resolve().parents[1]
NATIVE_EDGE_ROOT = Path(r"C:\Users\u\AppData\Local\Microsoft\Edge\User Data")


def _run_script(name: str) -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "tests" / name)],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tool_parser_holdback_script() -> None:
    _run_script("test_tool_parser_holdback.py")


def test_motion_no_standup_script() -> None:
    _run_script("test_motion_no_standup.py")


def test_tool_router_sticky_script() -> None:
    _run_script("test_tool_router_sticky.py")


def test_tool_router_cdp_routing_script() -> None:
    _run_script("test_tool_router_cdp_routing.py")


def test_engine_sticky_browser_cluster_script() -> None:
    _run_script("test_engine_sticky_browser_cluster.py")


def test_cancel_non_destructive_script() -> None:
    _run_script("test_cancel_non_destructive.py")


def test_browser_preflight_default_edge_needs_restart() -> None:
    procs = [
        RawProc(1, "msedge.exe", ["msedge.exe"]),
        RawProc(2, "msedge.exe", ["msedge.exe", "--type=renderer"]),
    ]
    state = infer_native_state(procs, NATIVE_EDGE_ROOT, 9222)
    assert state.running
    assert state.on_native_profile
    assert not state.has_debug_port
    assert needs_graceful_restart(state, cdp_alive=False, want_native=True) is True


def test_browser_preflight_debug_port_or_cdp_alive_skips_restart() -> None:
    state = infer_native_state(
        [RawProc(3, "msedge.exe", ["msedge.exe", "--remote-debugging-port=9222"])],
        NATIVE_EDGE_ROOT,
        9222,
    )
    assert state.has_debug_port
    assert needs_graceful_restart(state, cdp_alive=True, want_native=True) is False


def test_browser_preflight_no_edge_skips_restart() -> None:
    state = infer_native_state([], NATIVE_EDGE_ROOT, 9222)
    assert not state.running
    assert needs_graceful_restart(state, cdp_alive=False, want_native=True) is False


def test_browser_preflight_other_user_data_dir_skips_restart() -> None:
    state = infer_native_state(
        [RawProc(4, "msedge.exe", ["msedge.exe", "--user-data-dir=D:\\other"])],
        NATIVE_EDGE_ROOT,
        9222,
    )
    assert state.running
    assert not state.on_native_profile
    assert needs_graceful_restart(state, cdp_alive=False, want_native=True) is False


def test_browser_preflight_managed_mode_never_restarts() -> None:
    state = infer_native_state([RawProc(1, "msedge.exe", ["msedge.exe"])], NATIVE_EDGE_ROOT, 9222)
    assert needs_graceful_restart(state, cdp_alive=False, want_native=False) is False


def test_connect_or_launch_native_intent_downgrade(monkeypatch, tmp_path) -> None:
    from pawmate.tools.browser import playwright_runtime as browser
    from pawmate.tools.browser import cdp as cdp_attach

    class FakeAppPaths:
        downloads_dir = tmp_path / "downloads"
        data_root = tmp_path / "data"

    async def fake_ensure_playwright():
        return object()

    monkeypatch.setattr(browser, "get_app_paths", lambda: FakeAppPaths())
    monkeypatch.setattr(browser, "_ensure_playwright", fake_ensure_playwright)
    monkeypatch.setattr(cdp_attach, "probe_cdp", lambda _cdp_url: None)
    monkeypatch.setattr(cdp_attach, "any_edge_running", lambda: True)
    monkeypatch.setattr(cdp_attach, "launch_debug_edge_real_profile", lambda _port: (_ for _ in ()).throw(AssertionError("must not launch fallback")))

    settings = browser.BrowserUseSettings(
        attach_mode="attach",
        cdp_url="http://localhost:9222",
        profile_directory="native",
    )

    async def run() -> None:
        with pytest.raises(browser.BrowserIntentDowngrade) as raised:
            await browser._connect_or_launch(settings, "foreground")
        assert isinstance(raised.value, browser.BrowserDependencyError)
        assert raised.value.intent == "native"
        assert raised.value.fallback == "(none)"
        assert "PawMate 不会改用没有登录态的空浏览器" in str(raised.value)

    asyncio.run(run())


def test_connect_or_launch_managed_profile_does_not_preflight(monkeypatch, tmp_path) -> None:
    from pawmate.tools.browser import playwright_runtime as browser

    class FakeAppPaths:
        downloads_dir = tmp_path / "downloads"
        data_root = tmp_path / "data"

    async def fake_ensure_playwright():
        return object()

    async def fake_connect_cdp_session(*_args, **_kwargs):
        raise RuntimeError("managed attach unavailable")

    async def fake_wait_for_cdp_session(*_args, **_kwargs):
        return browser.BrowserSession(
            visibility="foreground",
            browser=object(),
            context=object(),
            attached=True,
        )

    async def fail_preflight(*_args, **_kwargs):
        raise AssertionError("managed profile path must not run native preflight")

    monkeypatch.setattr(browser, "get_app_paths", lambda: FakeAppPaths())
    monkeypatch.setattr(browser, "_ensure_playwright", fake_ensure_playwright)
    monkeypatch.setattr(browser, "_connect_cdp_session", fake_connect_cdp_session)
    monkeypatch.setattr(browser, "_wait_for_cdp_session", fake_wait_for_cdp_session)
    monkeypatch.setattr(browser, "_cdp_launch_profile_is_fresh", lambda _settings: False)
    monkeypatch.setattr(browser, "_launch_debuggable_browser", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser, "_preflight_native_profile", fail_preflight)

    settings = browser.BrowserUseSettings(
        attach_mode="prefer",
        cdp_url="http://127.0.0.1:9222",
        profile_directory="auto",
    )

    async def run() -> browser.BrowserSession:
        return await browser._connect_or_launch(settings, "foreground")

    session = asyncio.run(run())
    assert session.attached is True


def test_browser_open_reports_native_intent_downgrade(monkeypatch) -> None:
    from pawmate.tools.browser import playwright_runtime as browser

    async def fake_ensure_session(_visibility):
        raise browser.BrowserIntentDowngrade(
            "native unavailable",
            intent="native",
            fallback="managed",
        )

    monkeypatch.setattr(browser, "_ensure_session", fake_ensure_session)

    async def run() -> str:
        return await browser.browser_open("https://example.com")

    result = asyncio.run(run())
    assert "无法接管真实浏览器会话" in result
    assert "profile_directory 设置为 auto" in result
    assert "重新登录一次" in result


def test_browser_navigate_reports_native_intent_downgrade(monkeypatch) -> None:
    from pawmate.tools.browser import playwright_runtime as browser

    async def fake_get_active_page(*_args, **_kwargs):
        raise browser.BrowserIntentDowngrade(
            "native unavailable",
            intent="native",
            fallback="managed",
        )

    monkeypatch.setattr(browser, "_get_active_page", fake_get_active_page)

    async def run() -> str:
        return await browser.browser_navigate("https://example.com")

    result = asyncio.run(run())
    assert "无法接管真实浏览器会话" in result
    assert "关闭普通 Edge/Chrome" in result
