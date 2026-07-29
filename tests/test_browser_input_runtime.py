from __future__ import annotations

import asyncio

import pytest

from pawmate.tools.browser.input import (
    BackendUnsupported,
    BrowserActionContext,
    BrowserActionExecutor,
    BrowserInputBackend,
    CDPInputBackend,
    OSInputBackend,
    PlaywrightInputBackend,
    detect_human_verification,
)


class FailingBackend(BrowserInputBackend):
    def __init__(self, name: str, reason: str = "failed") -> None:
        self.backend_name = name
        self.reason = reason

    def can_handle(self, context: BrowserActionContext) -> bool:
        return True

    async def press_key(self, context: BrowserActionContext, key: str, payload: dict):
        raise BackendUnsupported(self.reason)


class KeyBackend(BrowserInputBackend):
    backend_name = "cdp"

    def can_handle(self, context: BrowserActionContext) -> bool:
        return True

    async def press_key(self, context: BrowserActionContext, key: str, payload: dict):
        return {"ok": True, "action": "key", "key": key}


def test_backend_fallback_playwright_to_cdp():
    executor = BrowserActionExecutor([FailingBackend("playwright", "page not available"), KeyBackend()])
    result = asyncio.run(executor.execute(BrowserActionContext(page=object()), {"action": "key", "key": "ArrowDown"}))

    assert result["ok"] is True
    assert result["backend"] == "cdp"
    assert result["fallbacks"] == [{"backend": "playwright", "ok": False, "reason": "page not available"}]


def test_backend_fallback_cdp_to_os_unsupported():
    executor = BrowserActionExecutor(
        [
            FailingBackend("playwright", "page not available"),
            FailingBackend("cdp", "cdp session unavailable"),
            OSInputBackend(),
        ]
    )
    result = asyncio.run(executor.execute(BrowserActionContext(page=object()), {"action": "key", "key": "Enter"}))

    assert result["ok"] is False
    assert result["error_type"] == "all_backends_failed"
    assert result["fallbacks"][-1]["backend"] == "os"
    assert result["fallbacks"][-1]["ok"] is False


class FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.pressed = []

    async def press(self, key: str):
        self.pressed.append(key)
        if key == "Backspace":
            self.page.text = ""

    async def insert_text(self, text: str):
        self.page.text += text


class FakeMouse:
    async def wheel(self, delta_x, delta_y):
        return None

    async def move(self, x, y):
        return None

    async def down(self):
        return None

    async def up(self):
        return None


class FakePlaywrightPage:
    def __init__(self):
        self.text = "old"
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse()

    async def wait_for_selector(self, ref: str, timeout: int = 0):
        return True

    async def click(self, ref: str):
        return True

    async def hover(self, ref: str):
        return True

    async def fill(self, ref: str, text: str):
        self.text = text

    async def evaluate(self, script: str, arg=None):
        if "captcha" in self.text.lower():
            return self.text
        if "submitReady" in script:
            return {"found": True, "text": self.text, "value": self.text, "submit_ready": True}
        return True


def test_smart_type_clear_true_reports_success():
    page = FakePlaywrightPage()
    executor = BrowserActionExecutor([PlaywrightInputBackend()])
    result = asyncio.run(
        executor.execute(
            BrowserActionContext(page=page),
            {"action": "smart_type", "ref": "#editor", "text": "hello", "clear": True},
        )
    )

    assert result["ok"] is True
    assert result["backend"] == "playwright"
    assert result["success"] is True
    assert result["after_text"] == "hello"


class FakeCdpClient:
    def __init__(self):
        self.calls = []

    async def send(self, method: str, params: dict):
        self.calls.append((method, params))
        return {}


class FakeCdpContext:
    def __init__(self, client: FakeCdpClient):
        self.client = client

    async def new_cdp_session(self, page):
        return self.client


class FakeCdpPage:
    def __init__(self, client: FakeCdpClient):
        self.context = FakeCdpContext(client)


def test_cdp_click_xy_dispatches_mouse_press_and_release():
    client = FakeCdpClient()
    backend = CDPInputBackend()
    result = asyncio.run(
        backend.click_xy(BrowserActionContext(page=FakeCdpPage(client)), 10, 20, "viewport", {"action": "click_xy"})
    )

    event_types = [params.get("type") for method, params in client.calls if method == "Input.dispatchMouseEvent"]
    assert result["ok"] is True
    assert "mousePressed" in event_types
    assert "mouseReleased" in event_types


class VerificationPage:
    def __init__(self, text: str):
        self.text = text

    async def evaluate(self, script: str):
        return self.text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("请输入短信验证码", "sms_verification_detected"),
        ("需要完成安全验证", "security_verification_detected"),
        ("captcha required", "captcha_detected"),
    ],
)
def test_human_verification_requires_handoff(text: str, reason: str):
    result = asyncio.run(detect_human_verification(VerificationPage(text)))

    assert result is not None
    assert result["ok"] is False
    assert result["error"] == "human_handoff_required"
    assert result["reason"] == reason
    assert result["can_resume"] is True


def test_native_session_disconnected_does_not_launch_managed(monkeypatch):
    from pawmate.tools.browser import playwright_runtime as browser

    class DisconnectedBrowser:
        def is_connected(self):
            return False

    async def fail_ensure_session(*_args, **_kwargs):
        raise AssertionError("must not launch managed browser")

    session = browser.BrowserSession(
        visibility="foreground",
        browser=DisconnectedBrowser(),
        context=object(),
        attached=True,
        mode="native",
        cdp_url="http://127.0.0.1:9222",
    )
    monkeypatch.setattr(browser, "_current_session", session)
    monkeypatch.setattr(browser, "_ensure_session", fail_ensure_session)

    result = asyncio.run(
        browser.browser_input_action({"action": "key", "key": "Enter", "_require_native_session": True})
    )

    assert result["ok"] is False
    assert result["error"] == "native_session_disconnected"
    assert result["session"]["mode"] == "native"


def test_cdp_connect_session_records_native_mode():
    from pawmate.tools.browser import playwright_runtime as browser

    class FakeContext:
        pass

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        async def connect_over_cdp(self, cdp_url: str):
            self.cdp_url = cdp_url
            return FakeBrowser()

    class FakePw:
        chromium = FakeChromium()

    session = asyncio.run(
        browser._connect_cdp_session(
            FakePw(),
            "http://127.0.0.1:9222",
            {},
            "foreground",
            settings=browser.BrowserUseSettings(browser="edge"),
            session_mode="native",
        )
    )

    assert session.mode == "native"
    assert session.cdp_url == "http://127.0.0.1:9222"
    assert session.browser_type == "edge"


def test_native_session_act_reuses_current_native_page(monkeypatch):
    from pawmate.tools.browser import playwright_runtime as browser

    class ConnectedBrowser:
        def is_connected(self):
            return True

    async def fail_ensure_session(*_args, **_kwargs):
        raise AssertionError("must reuse current native session")

    page = FakePlaywrightPage()
    session = browser.BrowserSession(
        visibility="foreground",
        browser=ConnectedBrowser(),
        context=object(),
        page=page,
        attached=True,
        mode="native",
        cdp_url="http://127.0.0.1:9222",
    )
    monkeypatch.setattr(browser, "_current_session", session)
    monkeypatch.setattr(browser, "_ensure_session", fail_ensure_session)

    result = asyncio.run(
        browser.browser_input_action({"action": "key", "key": "Enter", "_require_native_session": True})
    )

    assert result["ok"] is True
    assert result["backend"] == "playwright"
    assert result["session"]["mode"] == "native"
    assert page.keyboard.pressed == ["Enter"]
