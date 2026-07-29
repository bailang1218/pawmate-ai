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
from pawmate.core.safety.path_security import PathSecurityError
from pawmate.core.safety.security_service import SecurityService


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
_native_security_service = SecurityService()

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


def _get_clipboard_text() -> str | None:
    _win32api, _win32con, _win32gui, _win32process, win32clipboard, _image_grab = _require_windows_modules()
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return str(data) if data is not None else ""
        return None
    finally:
        win32clipboard.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    _win32api, _win32con, _win32gui, _win32process, win32clipboard, _image_grab = _require_windows_modules()
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, str(text))
    finally:
        win32clipboard.CloseClipboard()


def _paste_text_preserving_clipboard(text: str, *, submit: bool = False, address_bar: bool = False) -> bool:
    """Paste text and restore the previous Unicode clipboard when possible."""
    previous = _get_clipboard_text()
    restored = False
    try:
        _set_clipboard_text(text)
        if address_bar:
            _send_hotkey("ctrl+l")
        _send_hotkey("ctrl+v")
        if submit:
            _send_hotkey("enter")
    finally:
        if previous is not None:
            try:
                _set_clipboard_text(previous)
                restored = True
            except Exception:
                restored = False
    return restored


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
    clipboard_restored = await asyncio.to_thread(
        _paste_text_preserving_clipboard,
        url,
        submit=True,
        address_bar=True,
    )
    return {
        "ok": True,
        "operation": "native_browser_open_url",
        "url": url,
        "window": _serialize_window(window),
        "clipboard_restored": clipboard_restored,
    }


async def native_browser_type_text(
    text: str,
    submit: bool = False,
    window_id: int = 0,
    browser: str = "auto",
    title_contains: str = "",
) -> dict[str, Any]:
    window = await asyncio.to_thread(_select_window, window_id, browser, title_contains)
    await asyncio.to_thread(_focus_hwnd, window.hwnd)
    clipboard_restored = await asyncio.to_thread(
        _paste_text_preserving_clipboard,
        text,
        submit=submit,
        address_bar=False,
    )
    return {
        "ok": True,
        "operation": "native_browser_type_text",
        "submitted": submit,
        "window": _serialize_window(window),
        "text_length": len(text or ""),
        "clipboard_restored": clipboard_restored,
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
        try:
            target = Path(_native_security_service.check_path(str(Path(path).expanduser()), mode="write"))
        except PathSecurityError as exc:
            return {
                "ok": False,
                "operation": "native_browser_screenshot",
                "error_type": "security",
                "message": str(exc),
            }
    else:
        target = get_app_paths().downloads_dir / f"native_browser_{int(time.time())}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(image.save, str(target))
    return {"ok": True, "operation": "native_browser_screenshot", "path": str(target), "window": _serialize_window(window)}

# Native operations are internal fallbacks; they are not registered as model tools.
