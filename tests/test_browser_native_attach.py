from __future__ import annotations

import asyncio

import pytest


def test_normalize_cdp_url_rewrites_localhost():
    from pawmate.tools.browser import playwright_runtime as browser

    assert browser._normalize_cdp_url("localhost:9222") == "http://127.0.0.1:9222"
    assert browser._normalize_cdp_url("http://localhost:9222") == "http://127.0.0.1:9222"
    settings = browser.BrowserUseSettings()
    assert browser._cdp_url_for_settings(settings) == "http://127.0.0.1:19222"
    assert browser._DEFAULT_NATIVE_CDP_PORT == 9222


def test_wants_native_attach_matches_attach_or_native():
    from pawmate.tools.browser import playwright_runtime as browser

    assert browser._wants_native_attach(browser.BrowserUseSettings(attach_mode="attach"))
    assert browser._wants_native_attach(browser.BrowserUseSettings(profile_directory="native"))
    assert not browser._wants_native_attach(browser.BrowserUseSettings(attach_mode="managed", profile_directory="managed"))


def test_foreground_launch_args_use_app_mode(tmp_path):
    from pawmate.tools.browser import playwright_runtime as browser

    args = browser._persistent_context_args(browser.BrowserUseSettings(), str(tmp_path / "profile"))
    assert any(arg.startswith("--app=file:") and arg.endswith("/browser_bootstrap.html") for arg in args)
    assert "--app=about:blank" not in args
    assert "--start-minimized" in args
    assert "--window-position=-32000,-32000" in args
    assert "--new-window" not in args
    assert "--restore-last-session" not in args


@pytest.mark.asyncio
async def test_app_mode_reuses_blank_app_page_and_closes_stale_tabs():
    from pawmate.tools.browser import playwright_runtime as browser

    class FakePage:
        def __init__(self, url):
            self.url = url
            self.closed = False
            self.front = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

        async def bring_to_front(self):
            self.front = True

    app_page = FakePage("about:blank")
    stale_page = FakePage("https://example.com/old-tab")
    context = type("FakeContext", (), {"pages": [stale_page, app_page]})()
    session = browser.BrowserSession(
        visibility="foreground",
        browser=object(),
        context=context,
        app_mode=True,
    )

    selected = await browser._prepare_app_mode_session(session)

    assert selected is app_page
    assert session.page is app_page
    assert app_page.front is True
    assert stale_page.closed is True


@pytest.mark.asyncio
async def test_app_mode_does_not_create_a_normal_tab_when_page_is_missing(monkeypatch):
    from pawmate.tools.browser import playwright_runtime as browser

    class FakeContext:
        pages = []

        async def new_page(self):
            raise AssertionError("app mode must not create a normal browser tab")

    session = browser.BrowserSession(
        visibility="foreground",
        browser=object(),
        context=FakeContext(),
        app_mode=True,
    )

    async def fake_ensure_session(_visibility):
        return session

    monkeypatch.setattr(browser, "_ensure_session", fake_ensure_session)
    with pytest.raises(browser.BrowserDependencyError, match="app page is unavailable"):
        await browser._get_active_page(mode="foreground")


@pytest.mark.asyncio
async def test_close_session_closes_only_pawmate_owned_attached_browser():
    from pawmate.tools.browser import playwright_runtime as browser

    class FakeBrowser:
        def __init__(self):
            self.close_count = 0

        async def close(self):
            self.close_count += 1

    dedicated_browser = FakeBrowser()
    dedicated = browser.BrowserSession(
        visibility="foreground",
        browser=dedicated_browser,
        context=object(),
        attached=True,
        app_mode=True,
        mode="cdp_dedicated",
    )
    native_browser = FakeBrowser()
    native = browser.BrowserSession(
        visibility="foreground",
        browser=native_browser,
        context=object(),
        attached=True,
        mode="native",
    )

    assert await browser._close_session(dedicated) is True
    assert await browser._close_session(native) is True
    assert dedicated_browser.close_count == 1
    assert native_browser.close_count == 0


def test_debuggable_launch_uses_app_mode(monkeypatch, tmp_path):
    from pawmate.tools.browser import playwright_runtime as browser

    launched: dict[str, list[str]] = {}

    class FakePopen:
        def __init__(self, args, **_kwargs):
            launched["args"] = list(args)

    monkeypatch.setattr(browser, "_remote_debug_launch_candidates", lambda _settings: [r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"])
    monkeypatch.setattr(browser, "_cdp_profile_for_launch", lambda _settings: (tmp_path / "profile", "Default", False))
    monkeypatch.setattr(browser.subprocess, "Popen", FakePopen)

    assert browser._launch_debuggable_browser(
        browser.BrowserUseSettings(),
        "http://127.0.0.1:19222",
    )
    assert any(
        arg.startswith("--app=file:") and arg.endswith("/browser_bootstrap.html")
        for arg in launched["args"]
    )
    assert "--app=about:blank" not in launched["args"]
    assert "--start-minimized" in launched["args"]
    assert "--window-position=-32000,-32000" in launched["args"]
    assert "--new-window" not in launched["args"]
    assert "--restore-last-session" not in launched["args"]


def test_native_attach_failure_has_no_managed_fallback(monkeypatch, tmp_path):
    from pawmate.tools.browser import cdp as cdp_attach
    from pawmate.tools.browser import playwright_runtime as browser

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
    from pawmate.tools.browser import cdp as cdp_attach
    from pawmate.tools.browser import playwright_runtime as browser

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

    embedded: list[tuple[str, bool]] = []

    async def fake_request_embed(session):
        embedded.append((session.visibility, session.persistent_context))

    monkeypatch.setattr(browser, "get_app_paths", lambda: FakeAppPaths())
    monkeypatch.setattr(browser, "_ensure_playwright", fake_ensure_playwright)
    monkeypatch.setattr(browser, "_connect_cdp_session", fail_connect)
    monkeypatch.setattr(cdp_attach, "probe_cdp", lambda _url: (_ for _ in ()).throw(AssertionError("managed path must not probe CDP")))
    monkeypatch.setattr(browser, "_launch_persistent_context_with_fallback", fake_launch_persistent_context)
    monkeypatch.setattr(browser, "_request_embed_browser_surface", fake_request_embed)

    settings = browser.BrowserUseSettings(attach_mode="managed", profile_directory="managed")
    session = asyncio.run(browser._connect_or_launch(settings, "foreground"))
    assert session.persistent_context is True
    assert session.attached is False
    assert embedded == [("foreground", True)]


def test_facade_launch_managed_preserves_explicit_attach_settings(monkeypatch):
    from pawmate.tools.browser import facade as browser_facade
    from pawmate.tools.browser import playwright_runtime as browser

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
    monkeypatch.setattr(browser, "_browser_sessions", {})
    monkeypatch.setattr(browser, "_load_browser_settings", lambda: browser.BrowserUseSettings(attach_mode="attach", cdp_url="http://127.0.0.1:9222", profile_directory="native"))
    monkeypatch.setattr(browser, "_connect_or_launch", fake_connect_or_launch)

    adapters = browser_facade.build_adapters()
    asyncio.run(adapters.launch_managed())

    assert captured == {
        "attach_mode": "attach",
        "profile_directory": "native",
        "cdp_url": "http://127.0.0.1:9222",
    }


def test_facade_launch_managed_default_auto_uses_dedicated_profile_settings(monkeypatch):
    from pawmate.tools.browser import facade as browser_facade
    from pawmate.tools.browser import playwright_runtime as browser

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
    monkeypatch.setattr(browser, "_browser_sessions", {})
    monkeypatch.setattr(browser, "_load_browser_settings", lambda: browser.BrowserUseSettings())
    monkeypatch.setattr(browser, "_connect_or_launch", fake_connect_or_launch)

    adapters = browser_facade.build_adapters()
    asyncio.run(adapters.launch_managed())

    assert captured == {
        "attach_mode": "auto",
        "profile_directory": "auto",
        "cdp_url": "",
    }
