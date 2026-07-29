import importlib
from pathlib import Path


ROOT = Path("pawmate/tools")

REMOVED_ROOT_MODULES = [
    "browser_facade.py",
    "browser_input.py",
    "browser_preflight.py",
    "builtin_gateway.py",
    "cdp_attach.py",
    "chatgpt_bridge.py",
    "download_tool.py",
    "mcp_client.py",
    "mcp_server.py",
    "memory_tools.py",
    "native_browser.py",
    "playwright_browser.py",
    "registry.py",
    "sandbox.py",
    "scheduler_tools.py",
    "text_encoding.py",
    "unicode_debug.py",
    "vision_stub.py",
]

NEW_MODULES = [
    "pawmate.tools.core.registry",
    "pawmate.tools.gateway.builtin_gateway",
    "pawmate.tools.security.sandbox",
    "pawmate.tools.downloads.download_tool",
    "pawmate.tools.mcp.client",
    "pawmate.tools.mcp.server",
    "pawmate.tools.memory.tools",
    "pawmate.tools.scheduler.tools",
    "pawmate.tools.text.encoding",
    "pawmate.tools.text.unicode_debug",
    "pawmate.tools.chatgpt.bridge",
    "pawmate.tools.vision.stub",
    "pawmate.tools.browser.facade",
    "pawmate.tools.browser.input",
    "pawmate.tools.browser.preflight",
    "pawmate.tools.browser.cdp",
    "pawmate.tools.browser.native",
    "pawmate.tools.browser.playwright_runtime",
]


def test_tools_root_has_no_legacy_modules():
    remaining = [name for name in REMOVED_ROOT_MODULES if (ROOT / name).exists()]

    assert remaining == []


def test_tools_vnext_imports_new_modules():
    for name in NEW_MODULES:
        importlib.import_module(name)


def test_mcp_loader_uses_vnext_server_path():
    import pawmate.config as config
    from pawmate.core import mcp_loader

    expected = config.BASE_DIR / "tools" / "mcp" / "server.py"

    assert expected.exists()
    assert 'tools" / "mcp" / "server.py"' in Path(mcp_loader.__file__).read_text(encoding="utf-8")
