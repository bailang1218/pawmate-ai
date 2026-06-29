"""Windows native browser automation tools.

These tools operate on an already-open desktop browser window through Win32
focus, clipboard, keyboard, mouse, and screenshot APIs. They are intentionally
coarse compared with Playwright: use them when the user explicitly wants their
existing logged-in browser window, not a Playwright-controlled browser context.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pawmate.storage.app_paths import get_app_paths
from pawmate.tools.registry import APPROVAL_CONFIRM, APPROVAL_NOTIFY, ToolDef, ToolRegistry


@dataclass(frozen=True)
class NativeWindow:
    hwnd: int
    title: str
    class_name: str
    process_path: str
    rect: tuple[int, int, int, int]
    foreground: bool


_BROWSER_EXE_ALIASES: dict[str, set[str]] = {
    "edge": {"msedge.exe"},
    "chrome": {"chrome.exe"},
    "google": {"chrome.exe"},
    "firefox": {"firefox.exe"},
    "2345": {"2345explorer.exe", "2345chrome.exe"},
    "2345explorer": {"2345explorer.exe", "2345chrome.exe"},
    "baidu": {"baidubrowser.exe", "spark.exe"},
    "qq": {"qqbrowser.exe"},
    "qqbrowser": {"qqbrowser.exe"},
    "sogou": {"sogouexplorer.exe"},
    "360": {"360se.exe", "360chrome.exe", "360chrome x.exe", "360ee.exe"},
    "360se": {"360se.exe"},
    "360chrome": {"360chrome.exe", "360chrome x.exe"},
    "liebao": {"liebao.exe"},
    "maxthon": {"maxthon.exe"},
    "brave": {"brave.exe"},
    "vivaldi": {"vivaldi.exe"},
    "opera": {"opera.exe", "opera_gx.exe"},
}
_CHROMIUM_WINDOW_CLASSES = {"Chrome_WidgetWin_1"}
_BROWSER_WINDOW_CLASSES = _CHROMIUM_WINDOW_CLASSES | {"MozillaWindowClass"}
_AUTOMATION_LEVELS = {"conservative", "standard", "aggressive"}
_AUTOMATION_LEVEL_BLOCKED = {
    "ok": False,
    "operation": "native_browser_click",
    "error_type": "automation_level_blocked",
    "message": "当前接管级别不允许直接点击屏幕兜底。请改用 selector 路线，或向用户说明并建议在设置里调高接管级别。",
}


def _load_automation_level() -> str:
    try:
        import pawmate.config as config

        config_path = Path(config.BASE_DIR) / "config.json"
        if not config_path.exists():
            return "standard"
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        raw = loaded.get("tools", {}).get("browser_use", {}) if isinstance(loaded, dict) else {}
        level = str(raw.get("automation_level", "standard") or "standard").lower() if isinstance(raw, dict) else "standard"
    except Exception:
        return "standard"
    return level if level in _AUTOMATION_LEVELS else "standard"


def _require_windows_modules():
    try:
        import win32api
        import win32con
        import win32gui
        import win32clipboard
        import win32process
        from PIL import ImageGrab
    except Exception as exc:  # pragma: no cover - platform/dependency guard
        raise RuntimeError(f"native browser automation is only available on Windows with pywin32/Pillow: {exc}") from exc
    return win32api, win32con, win32gui, win32process, win32clipboard, ImageGrab


def _process_path_for_hwnd(hwnd: int) -> str:
    win32api, win32con, _win32gui, win32process, _win32clipboard, _image_grab = _require_windows_modules()
    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            return win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return ""


def _browser_exe_names(browser: str) -> set[str]:
    browser = (browser or "auto").strip().lower()
    if browser == "auto":
        names: set[str] = set()
        for values in _BROWSER_EXE_ALIASES.values():
            names.update(values)
        return names
    if browser.endswith(".exe"):
        return {browser}
    if browser in _BROWSER_EXE_ALIASES:
        return set(_BROWSER_EXE_ALIASES[browser])
    return {f"{browser}.exe"}


def _enumerate_browser_windows(browser: str = "auto") -> list[NativeWindow]:
    _win32api, _win32con, win32gui, _win32process, _win32clipboard, _image_grab = _require_windows_modules()
    exe_names = _browser_exe_names(browser)
    foreground = win32gui.GetForegroundWindow()
    windows: list[NativeWindow] = []

    def visit(hwnd: int, _param: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            class_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        if class_name not in _BROWSER_WINDOW_CLASSES:
            return True
        if rect[2] - rect[0] < 240 or rect[3] - rect[1] < 160:
            return True
        path = _process_path_for_hwnd(hwnd)
        if Path(path).name.lower() not in exe_names:
            return True
        windows.append(
            NativeWindow(
                hwnd=hwnd,
                title=title,
                class_name=class_name,
                process_path=path,
                rect=rect,
                foreground=hwnd == foreground,
            )
        )
        return True

    win32gui.EnumWindows(visit, None)
    windows.sort(
        key=lambda w: (
            not w.foreground,
            -(w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]),
            w.hwnd,
        )
    )
    return windows


def _serialize_window(window: NativeWindow) -> dict[str, Any]:
    left, top, right, bottom = window.rect
    return {
        "id": window.hwnd,
        "title": window.title,
        "class_name": window.class_name,
        "process_path": window.process_path,
        "rect": {"left": left, "top": top, "right": right, "bottom": bottom, "width": right - left, "height": bottom - top},
        "foreground": window.foreground,
    }


def _select_window(window_id: int = 0, browser: str = "auto", title_contains: str = "") -> NativeWindow:
    windows = _enumerate_browser_windows(browser)
    if window_id:
        for window in windows:
            if window.hwnd == int(window_id):
                return window
        raise RuntimeError(f"native browser window not found: {window_id}")
    needle = (title_contains or "").strip().lower()
    if needle:
        for window in windows:
            if needle in window.title.lower():
                return window
        raise RuntimeError(f"native browser window not found with title containing: {title_contains}")
    if not windows:
        raise RuntimeError(f"no visible native {browser} browser window found")
    return windows[0]


def _focus_hwnd(hwnd: int) -> None:
    win32api, win32con, win32gui, _win32process, _win32clipboard, _image_grab = _require_windows_modules()
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Windows foreground lock can reject direct focus. A harmless Alt pulse
        # usually grants this process permission to set the foreground window.
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.15)


def _set_clipboard_text(text: str) -> None:
    _win32api, _win32con, _win32gui, _win32process, win32clipboard, _image_grab = _require_windows_modules()
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, str(text))
    finally:
        win32clipboard.CloseClipboard()


def _send_hotkey(keys: str) -> None:
    win32api, win32con, _win32gui, _win32process, _win32clipboard, _image_grab = _require_windows_modules()
    key_map = {
        "ctrl": win32con.VK_CONTROL,
        "control": win32con.VK_CONTROL,
        "shift": win32con.VK_SHIFT,
        "alt": win32con.VK_MENU,
        "enter": win32con.VK_RETURN,
        "return": win32con.VK_RETURN,
        "tab": win32con.VK_TAB,
        "esc": win32con.VK_ESCAPE,
        "escape": win32con.VK_ESCAPE,
        "backspace": win32con.VK_BACK,
        "space": win32con.VK_SPACE,
        "left": win32con.VK_LEFT,
        "right": win32con.VK_RIGHT,
        "up": win32con.VK_UP,
        "down": win32con.VK_DOWN,
        "home": win32con.VK_HOME,
        "end": win32con.VK_END,
        "delete": win32con.VK_DELETE,
        "del": win32con.VK_DELETE,
    }
    parts = [p.strip().lower() for p in keys.replace("+", " ").split() if p.strip()]
    vk_codes: list[int] = []
    for part in parts:
        if len(part) == 1:
            vk_codes.append(ord(part.upper()))
        elif part.startswith("f") and part[1:].isdigit():
            vk_codes.append(win32con.VK_F1 + int(part[1:]) - 1)
        elif part in key_map:
            vk_codes.append(key_map[part])
        else:
            raise ValueError(f"unsupported key: {part}")
    for code in vk_codes:
        win32api.keybd_event(code, 0, 0, 0)
    for code in reversed(vk_codes):
        win32api.keybd_event(code, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


async def native_browser_list_windows(browser: str = "auto") -> dict[str, Any]:
    windows = await asyncio.to_thread(_enumerate_browser_windows, browser)
    return {"ok": True, "operation": "native_browser_list_windows", "windows": [_serialize_window(w) for w in windows]}


async def native_browser_focus(window_id: int = 0, browser: str = "auto", title_contains: str = "") -> dict[str, Any]:
    window = await asyncio.to_thread(_select_window, window_id, browser, title_contains)
    await asyncio.to_thread(_focus_hwnd, window.hwnd)
    refreshed = _select_window(window.hwnd, browser, "")
    return {"ok": True, "operation": "native_browser_focus", "window": _serialize_window(refreshed)}


async def native_browser_open_url(url: str, window_id: int = 0, browser: str = "auto", title_contains: str = "") -> dict[str, Any]:
    if not url:
        return {"ok": False, "operation": "native_browser_open_url", "message": "url is required"}
    window = await asyncio.to_thread(_select_window, window_id, browser, title_contains)
    await asyncio.to_thread(_focus_hwnd, window.hwnd)
    await asyncio.to_thread(_set_clipboard_text, url)
    await asyncio.to_thread(_send_hotkey, "ctrl+l")
    await asyncio.to_thread(_send_hotkey, "ctrl+v")
    await asyncio.to_thread(_send_hotkey, "enter")
    return {"ok": True, "operation": "native_browser_open_url", "url": url, "window": _serialize_window(window)}


async def native_browser_type_text(
    text: str,
    submit: bool = False,
    window_id: int = 0,
    browser: str = "auto",
    title_contains: str = "",
) -> dict[str, Any]:
    window = await asyncio.to_thread(_select_window, window_id, browser, title_contains)
    await asyncio.to_thread(_focus_hwnd, window.hwnd)
    await asyncio.to_thread(_set_clipboard_text, text)
    await asyncio.to_thread(_send_hotkey, "ctrl+v")
    if submit:
        await asyncio.to_thread(_send_hotkey, "enter")
    return {
        "ok": True,
        "operation": "native_browser_type_text",
        "submitted": submit,
        "window": _serialize_window(window),
        "text_length": len(text or ""),
    }


async def native_browser_hotkey(
    keys: str,
    window_id: int = 0,
    browser: str = "auto",
    title_contains: str = "",
) -> dict[str, Any]:
    window = await asyncio.to_thread(_select_window, window_id, browser, title_contains)
    await asyncio.to_thread(_focus_hwnd, window.hwnd)
    await asyncio.to_thread(_send_hotkey, keys)
    return {"ok": True, "operation": "native_browser_hotkey", "keys": keys, "window": _serialize_window(window)}


async def native_browser_click(
    x: int,
    y: int,
    window_id: int = 0,
    browser: str = "auto",
    title_contains: str = "",
    relative: bool = True,
) -> dict[str, Any]:
    if _load_automation_level() != "aggressive":
        return dict(_AUTOMATION_LEVEL_BLOCKED)
    win32api, win32con, _win32gui, _win32process, _win32clipboard, _image_grab = _require_windows_modules()
    window = await asyncio.to_thread(_select_window, window_id, browser, title_contains)
    await asyncio.to_thread(_focus_hwnd, window.hwnd)
    left, top, _right, _bottom = window.rect
    sx = left + int(x) if relative else int(x)
    sy = top + int(y) if relative else int(y)
    await asyncio.to_thread(win32api.SetCursorPos, (sx, sy))
    await asyncio.to_thread(win32api.mouse_event, win32con.MOUSEEVENTF_LEFTDOWN, sx, sy, 0, 0)
    await asyncio.to_thread(win32api.mouse_event, win32con.MOUSEEVENTF_LEFTUP, sx, sy, 0, 0)
    return {
        "ok": True,
        "operation": "native_browser_click",
        "message": "正在以模拟点击方式兜底操作你的浏览器。",
        "screen": {"x": sx, "y": sy},
        "window": _serialize_window(window),
    }


async def native_browser_screenshot(window_id: int = 0, browser: str = "auto", title_contains: str = "", path: str = "") -> dict[str, Any]:
    _win32api, _win32con, _win32gui, _win32process, _win32clipboard, ImageGrab = _require_windows_modules()
    window = await asyncio.to_thread(_select_window, window_id, browser, title_contains)
    left, top, right, bottom = window.rect
    image = await asyncio.to_thread(ImageGrab.grab, (left, top, right, bottom))
    if path:
        target = Path(path).expanduser()
    else:
        target = get_app_paths().downloads_dir / f"native_browser_{int(time.time())}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(image.save, str(target))
    return {"ok": True, "operation": "native_browser_screenshot", "path": str(target), "window": _serialize_window(window)}


def register_native_browser_tools(registry: ToolRegistry) -> None:
    """Register native desktop browser tools."""
    browser_schema = {
        "type": "string",
        "default": "auto",
        "description": "auto scans common installed browsers. You can also pass edge, chrome, firefox, 2345, baidu, qq, sogou, 360, brave, vivaldi, opera, or a browser exe name.",
    }
    tools = [
        ToolDef(
            name="native_browser_list_windows",
            description="列出当前已打开的原生浏览器窗口（auto 会扫描 Edge/Chrome/Firefox/2345/百度/QQ/搜狗/360 等常见浏览器）。用户要求操作自己的浏览器、原生浏览器、已有登录态窗口时，先用它选择窗口；不要用 browser_open 新开 Playwright 窗口。",
            input_schema={
                "type": "object",
                "properties": {"browser": browser_schema},
            },
            handler=native_browser_list_windows,
        ),
        ToolDef(
            name="native_browser_focus",
            description="聚焦一个已打开的原生浏览器窗口，不创建新浏览器。适合操作用户已有登录信息的窗口。",
            input_schema={
                "type": "object",
                "properties": {
                    "window_id": {"type": "integer", "default": 0},
                    "browser": browser_schema,
                    "title_contains": {"type": "string", "default": ""},
                },
            },
            handler=native_browser_focus,
            approval=APPROVAL_NOTIFY,
        ),
        ToolDef(
            name="native_browser_open_url",
            description="在用户已打开的原生浏览器窗口里用地址栏打开 URL。不会创建 Playwright 自带窗口，保留用户登录态。",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "window_id": {"type": "integer", "default": 0},
                    "browser": browser_schema,
                    "title_contains": {"type": "string", "default": ""},
                },
                "required": ["url"],
            },
            handler=native_browser_open_url,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="native_browser_type_text",
            description="向当前聚焦的原生浏览器输入框粘贴文本，可选择回车提交。用于用户自己的已登录浏览器窗口；需要先 focus/click 到目标输入框。",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "submit": {"type": "boolean", "default": False},
                    "window_id": {"type": "integer", "default": 0},
                    "browser": browser_schema,
                    "title_contains": {"type": "string", "default": ""},
                },
                "required": ["text"],
            },
            handler=native_browser_type_text,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="native_browser_hotkey",
            description="向原生浏览器窗口发送热键，例如 ctrl+l、ctrl+f、enter、tab。用于已有浏览器窗口，不创建 Playwright 窗口。",
            input_schema={
                "type": "object",
                "properties": {
                    "keys": {"type": "string"},
                    "window_id": {"type": "integer", "default": 0},
                    "browser": browser_schema,
                    "title_contains": {"type": "string", "default": ""},
                },
                "required": ["keys"],
            },
            handler=native_browser_hotkey,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="native_browser_click",
            description=(
                "最后兜底：按坐标点击原生浏览器窗口。x/y 默认是相对窗口左上角坐标。"
                "普通网页交互优先用 Playwright/CDP 的 browser_open -> browser_observe -> browser_click，"
                "不要优先用视觉识别坐标点击。仅在用户明确要求操作已有原生窗口、CDP/selector 不可用，"
                "且已经通过 native_browser_screenshot 确认目标坐标后使用。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "window_id": {"type": "integer", "default": 0},
                    "browser": browser_schema,
                    "title_contains": {"type": "string", "default": ""},
                    "relative": {"type": "boolean", "default": True},
                },
                "required": ["x", "y"],
            },
            handler=native_browser_click,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="native_browser_screenshot",
            description=(
                "截取原生浏览器窗口截图，帮助确认当前页面和坐标。用于已有浏览器窗口，不创建 Playwright 窗口。"
                "这是原生坐标兜底流程的一部分；普通网页操作应先尝试 Playwright/CDP selector 路线。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "window_id": {"type": "integer", "default": 0},
                    "browser": browser_schema,
                    "title_contains": {"type": "string", "default": ""},
                    "path": {"type": "string", "default": ""},
                },
            },
            handler=native_browser_screenshot,
            approval=APPROVAL_NOTIFY,
        ),
    ]
    registry.register_bulk({tool.name: tool for tool in tools})
