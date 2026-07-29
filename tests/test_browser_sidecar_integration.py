from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_chat_browser_uses_a_dedicated_sidecar_slot():
    index_html = (ROOT / "pawmate/ui/web/index.html").read_text(encoding="utf-8")
    workspace_js = (ROOT / "pawmate/ui/web/js/workspace.js").read_text(encoding="utf-8")
    workspace_css = (ROOT / "pawmate/ui/web/css/workspace.css").read_text(encoding="utf-8")

    assert 'id="browserSidecar"' in index_html
    assert 'id="browserSidecarResizer"' in index_html
    assert "data-browser-sidecar-stage" in index_html
    assert "function showBrowserSidecar()" in workspace_js
    assert "function finishBrowserSidecarReveal()" in workspace_js
    assert "finishBrowserSidecarReveal" in workspace_js
    assert 'event.propertyName === "transform"' in workspace_js
    assert "function startBrowserSidecarResize(event)" in workspace_js
    assert 'localStorage.setItem("pawmate.browserSidecarWidth"' in workspace_js
    assert 'setBrowserHostVisibility(false)' in workspace_js
    assert 'active === "chat" && browserSidecarOpen' in workspace_js
    assert ".app-shell.browser-sidecar-open .chat-panel" in workspace_css
    assert "cursor:col-resize" in workspace_css
    assert ".browser-sidecar.is-entering" in workspace_css
    assert "transition:transform .26s" in workspace_css
    assert ".browser-sidecar[hidden]" in workspace_css


def test_embed_request_waits_for_web_reported_host_geometry():
    embedded_browser = (ROOT / "pawmate/ui/embedded_browser.py").read_text(encoding="utf-8")
    web_window = (ROOT / "pawmate/ui/web_chat_window.py").read_text(encoding="utf-8")

    embed_body = embedded_browser.split("def embed_cdp_browser(", 1)[1].split(
        "def _try_attach(", 1
    )[0]
    assert "self.set_workspace_visible(True)" not in embed_body
    assert "embedRequested.connect(self._show_browser_sidecar)" in web_window
    assert "showBrowserSidecar" in web_window


def test_native_browser_rechecks_layout_and_window_style_after_attach():
    embedded_browser = (ROOT / "pawmate/ui/embedded_browser.py").read_text(encoding="utf-8")

    assert "def _schedule_browser_resize(" in embedded_browser
    assert "for delay_ms in (0, 40, 120, 280)" in embedded_browser
    assert "def _enforce_hosted_window_style(" in embedded_browser
    assert "SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW" in embedded_browser
    assert "self._geometry_guard.setInterval(120)" in embedded_browser
    assert "self._geometry_guard.timeout.connect(self._resize_browser)" in embedded_browser


def test_web_modal_temporarily_hides_native_browser_airspace():
    workspace_js = (ROOT / "pawmate/ui/web/js/workspace.js").read_text(encoding="utf-8")
    tools_js = (ROOT / "pawmate/ui/web/js/chat/tools.js").read_text(encoding="utf-8")

    assert "function setBrowserHostObscured(obscured)" in workspace_js
    assert "if (browserHostObscured) visible = false" in workspace_js
    assert "setBrowserHostObscured: setBrowserHostObscured" in workspace_js
    assert "setBrowserHostObscured(true)" in tools_js
    assert "setBrowserHostObscured(false)" in tools_js


def test_window_restore_remounts_webengine_surface_and_resyncs_native_host():
    web_window = (ROOT / "pawmate/ui/web_chat_window.py").read_text(encoding="utf-8")
    web_view = (ROOT / "pawmate/ui/web_chat_view.py").read_text(encoding="utf-8")

    assert "QEvent.Type.WindowStateChange" in web_window
    assert "QEvent.Type.ActivationChange" in web_window
    assert "recover_render_surface" in web_window
    assert "self._apply_browser_host_geometry()" in web_window
    assert "def recover_render_surface(" in web_view
    assert "web_view.hide()" in web_view
    assert "web_view.show()" in web_view


def test_webengine_surface_recovery_remounts_without_reloading_page():
    from pawmate.ui.web_chat_view import WebChatView

    calls = []

    class FakePage:
        def runJavaScript(self, script):  # noqa: N802 - Qt API
            calls.append(("javascript", script))

    class FakeWebView:
        def hide(self):
            calls.append("hide")

        def show(self):
            calls.append("show")

        def updateGeometry(self):  # noqa: N802 - Qt API
            calls.append("geometry")

        def update(self):
            calls.append("update")

        def page(self):
            return FakePage()

    WebChatView.recover_render_surface(
        SimpleNamespace(_web_view=FakeWebView()),
        remount=True,
    )

    assert calls[:4] == ["hide", "show", "geometry", "update"]
    assert calls[4][0] == "javascript"
