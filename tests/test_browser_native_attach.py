from __future__ import annotations

import asyncio

import pytest


def test_normalize_cdp_url_rewrites_localhost():
    from pawmate.tools import playwright_browser as browser

    assert browser._normalize_cdp_url("localhost:9222") == "http://127.0.0.1:9222"
    assert browser._normalize_cdp_url("http://localhost:9222") == "http://127.0.0.1:9222"
    settings = browser.BrowserUseSettings()
    assert browser._cdp_url_for_settings(settings) == "http://127.0.0.1:9222"


def test_wants_native_attach_matches_attach_or_native():
    from pawmate.tools import playwright_browser as browser

    assert browser._wants_native_attach(browser.BrowserUseSettings(attach_mode="attach"))
    assert browser._wants_native_attach(browser.BrowserUseSettings(profile_directory="native"))
    assert not browser._wants_native_attach(browser.BrowserUseSettings(attach_mode="managed", profile_directory="managed"))


def test_native_attach_failure_has_no_managed_fallback(monkeypatch, tmp_path):
    from pawmate.tools import cdp_attach
    from pawmate.tools import playwright_browser as browser

    class FakeAppPaths:
        downloads_dir = tmp_path / "downloads"
        data_root = tmp_path / "data"

    seen: dict[str, str] = {}

    async def fake_ensure_playwright():
        return object()

    def fake_probe(cdp_url: str):
        seen["cdp_url"] = cdp_url
        return None

    async def fail_connect(*_args, **_kwargs):
        raise AssertionError("native failure must stop before connect/fallback")

    monkeypatch.setattr(browser, "get_app_paths", lambda: FakeAppPaths())
    monkeypatch.setattr(browser, "_ensure_playwright", fake_ensure_playwright)
    monkeypatch.setattr(browser, "_connect_cdp_session", fail_connect)
    monkeypatch.setattr(cdp_attach, "probe_cdp", fake_probe)
    monkeypatch.setattr(cdp_attach, "any_edge_running", lambda: True)
    monkeypatch.setattr(cdp_attach, "launch_debug_edge_real_profile", lambda _port: (_ for _ in ()).throw(AssertionError("must not launch")))

    settings = browser.BrowserUseSettings(
        attach_mode="attach",
        cdp_url="http://localhost:9222",
        profile_directory="native",
    )

    async def run():
        with pytest.raises(browser.BrowserIntentDowngrade) as raised:
            await browser._connect_or_launch(settings, "foreground")
        assert raised.value.intent == "native"
        assert raised.value.fallback == "(none)"

    asyncio.run(run())
    assert seen["cdp_url"] == "http://127.0.0.1:9222"


def test_managed_connect_or_launch_skips_cdp(monkeypatch, tmp_path):
    from pawmate.tools import cdp_attach
    from pawmate.tools import playwright_browser as browser

    class FakeAppPaths:
        downloads_dir = tmp_path / "downloads"
        data_root = tmp_path / "data"

    class FakeContext:
        browser = object()

    async def fake_ensure_playwright():
        return object()

    async def fail_connect(*_args, **_kwargs):
        raise AssertionError("managed path must not connect to CDP")

    async def fake_launch_persistent_context(*_args, **_kwargs):
        return FakeContext()

    monkeypatch.setattr(browser, "get_app_paths", lambda: FakeAppPaths())
    monkeypatch.setattr(browser, "_ensure_playwright", fake_ensure_playwright)
    monkeypatch.setattr(browser, "_connect_cdp_session", fail_connect)
    monkeypatch.setattr(cdp_attach, "probe_cdp", lambda _url: (_ for _ in ()).throw(AssertionError("managed path must not probe CDP")))
    monkeypatch.setattr(browser, "_launch_persistent_context_with_fallback", fake_launch_persistent_context)

    settings = browser.BrowserUseSettings(attach_mode="managed", profile_directory="managed")
    session = asyncio.run(browser._connect_or_launch(settings, "foreground"))
    assert session.persistent_context is True
    assert session.attached is False


def test_facade_launch_managed_forces_managed_settings(monkeypatch):
    from pawmate.tools import browser_facade
    from pawmate.tools import playwright_browser as browser

    captured = {}

    async def fake_connect_or_launch(settings, visibility):
        captured["attach_mode"] = settings.attach_mode
        captured["profile_directory"] = settings.profile_directory
        captured["cdp_url"] = settings.cdp_url
        return browser.BrowserSession(
            visibility=visibility,
            browser=object(),
            context=object(),
            attached=False,
        )

    monkeypatch.setattr(browser, "_current_session", None)
    monkeypatch.setattr(browser, "_load_browser_settings", lambda: browser.BrowserUseSettings(attach_mode="attach", cdp_url="http://127.0.0.1:9222", profile_directory="native"))
    monkeypatch.setattr(browser, "_connect_or_launch", fake_connect_or_launch)

    adapters = browser_facade.build_adapters()
    asyncio.run(adapters.launch_managed())

    assert captured == {
        "attach_mode": "managed",
        "profile_directory": "managed",
        "cdp_url": "",
    }

