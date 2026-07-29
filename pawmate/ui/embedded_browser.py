"""Host PawMate's dedicated Playwright browser inside the automation cockpit.

Qt WebEngine is not used as the automation target.  Playwright continues to
own a dedicated Chromium/Edge process through CDP; on Windows this module only
reparents that process' native window into a QWidget owned by PawMate.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import weakref
from ctypes import wintypes
from urllib.parse import urlparse

from pawmate.qt_compat import QApplication, QLabel, QTimer, Qt, QVBoxLayout, QWidget, Signal


logger = logging.getLogger("pawmate.browser")
_workspace_ref: weakref.ReferenceType["EmbeddedBrowserWorkspace"] | None = None
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if sys.platform == "win32" else None


class EmbeddedBrowserWorkspace(QWidget):
    """A native host surface for the dedicated, CDP-controlled browser."""

    embedRequested = Signal(str, bool, int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        global _workspace_ref
        _workspace_ref = weakref.ref(self)

        self.setObjectName("EmbeddedBrowserWorkspace")
        self.setStyleSheet(
            "#EmbeddedBrowserWorkspace { background:#f7fbfd; border:1px solid #d7e9ef; }"
            "#EmbeddedBrowserStatus { color:#6d8791; background:#f7fbfd; padding:20px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._status = QLabel("开始任务后，PawMate 专用浏览器会显示在这里。", self)
        self._status.setObjectName("EmbeddedBrowserStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._host = QWidget(self)
        self._host.setObjectName("EmbeddedBrowserNativeHost")
        self._host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._host.setStyleSheet("#EmbeddedBrowserNativeHost { background:#ffffff; }")
        self._host.hide()
        layout.addWidget(self._host, 1)

        self._browser_hwnd = 0
        self._original_parent = 0
        self._original_style = 0
        self._original_ex_style = 0
        self._hosted_style = 0
        self._hosted_ex_style = 0
        self._pending_cdp_url = ""
        self._pending_process_id = 0
        self._attach_attempts = 0
        self._workspace_visible = False
        self.embedRequested.connect(self.embed_cdp_browser)
        self._geometry_guard = QTimer(self)
        self._geometry_guard.setInterval(120)
        self._geometry_guard.timeout.connect(self._resize_browser)

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.detach_browser)
        self.hide()

    def set_workspace_visible(self, visible: bool) -> None:
        self._workspace_visible = bool(visible)
        if visible:
            self.show()
            self.raise_()
            self._geometry_guard.start()
            self._schedule_browser_resize()
        else:
            self._geometry_guard.stop()
            self.hide()

    def embed_cdp_browser(self, cdp_url: str, visible: bool = True, process_id: int = 0) -> None:
        """Attach the browser exposing ``cdp_url`` without blocking Qt."""
        if sys.platform != "win32":
            self._show_status("浏览器内嵌目前仅支持 Windows。")
            return
        self._pending_cdp_url = str(cdp_url or "").strip()
        self._pending_process_id = max(0, int(process_id or 0))
        self._attach_attempts = 0
        if self._browser_hwnd and not _is_window(self._browser_hwnd):
            self._clear_attached_state()
        if self._browser_hwnd:
            self._show_browser_surface()
            return
        self._show_status("正在连接 PawMate 专用浏览器…")
        QTimer.singleShot(0, self._try_attach)

    def _try_attach(self) -> None:
        if self._browser_hwnd:
            self._resize_browser()
            return
        port = _cdp_port(self._pending_cdp_url)
        hwnd = _find_browser_window(port, self._pending_process_id) if (port or self._pending_process_id) else 0
        if hwnd:
            try:
                self._attach_hwnd(hwnd)
                return
            except Exception as exc:
                logger.warning("Failed to host Playwright browser hwnd=%s: %s", hwnd, exc)
        self._attach_attempts += 1
        if self._attach_attempts < 60:
            QTimer.singleShot(250, self._try_attach)
            return
        self._show_status("浏览器已启动，但暂时没有找到可嵌入的窗口。可在浏览器设置中重试。")

    def _attach_hwnd(self, hwnd: int) -> None:
        user32 = _user32()
        # A newly launched Edge window can become visible briefly before Qt
        # reparents it. Hide it first; the hosted child is shown after SetParent.
        user32.ShowWindow(hwnd, 0)
        self._original_parent = int(user32.GetParent(hwnd) or 0)
        self._original_style = int(user32.GetWindowLongPtrW(hwnd, -16))
        self._original_ex_style = int(user32.GetWindowLongPtrW(hwnd, -20))

        # Strip the top-level frame and make the browser a visible child of the
        # QWidget host. Playwright/CDP ownership is otherwise untouched.
        frame_flags = (
            0x80000000  # WS_POPUP
            | 0x00C00000  # WS_CAPTION
            | 0x00040000  # WS_THICKFRAME
            | 0x00080000  # WS_SYSMENU
            | 0x00020000  # WS_MINIMIZEBOX
            | 0x00010000  # WS_MAXIMIZEBOX
        )
        child_style = (self._original_style & ~frame_flags) | 0x40000000 | 0x10000000 | 0x02000000 | 0x04000000
        ex_frame_flags = 0x00040000 | 0x00000100 | 0x00000200 | 0x00000001
        self._hosted_style = child_style
        self._hosted_ex_style = self._original_ex_style & ~ex_frame_flags
        user32.SetWindowLongPtrW(hwnd, -16, self._hosted_style)
        user32.SetWindowLongPtrW(hwnd, -20, self._hosted_ex_style)
        if not user32.SetParent(hwnd, int(self._host.winId())):
            error = ctypes.get_last_error()
            # SetParent returns the previous parent, which is legitimately NULL
            # for a top-level window. Only fail if Windows recorded an error.
            if error:
                raise OSError(error, "SetParent failed")
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        user32.ShowWindow(hwnd, 5)
        self._browser_hwnd = int(hwnd)
        self._status.hide()
        self._host.show()
        self._host.raise_()
        self._schedule_browser_resize()
        logger.info("Hosted PawMate dedicated browser hwnd=%s", hwnd)

    def _show_browser_surface(self) -> None:
        self._status.hide()
        self._host.show()
        self._host.raise_()
        try:
            _user32().ShowWindow(self._browser_hwnd, 5)
        except Exception:
            pass
        self._schedule_browser_resize()

    def _show_status(self, message: str) -> None:
        self._host.hide()
        self._status.setText(message)
        self._status.show()

    def _schedule_browser_resize(self) -> None:
        """Resize again after Qt and Chromium have both settled their layout."""
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self._host.updateGeometry()
        self._resize_browser()
        for delay_ms in (0, 40, 120, 280):
            QTimer.singleShot(delay_ms, self._resize_browser)

    def _enforce_hosted_window_style(self, hwnd: int, user32) -> None:
        if not self._hosted_style:
            return
        current_style = int(user32.GetWindowLongPtrW(hwnd, -16))
        current_ex_style = int(user32.GetWindowLongPtrW(hwnd, -20))
        if current_style != self._hosted_style:
            user32.SetWindowLongPtrW(hwnd, -16, self._hosted_style)
        if current_ex_style != self._hosted_ex_style:
            user32.SetWindowLongPtrW(hwnd, -20, self._hosted_ex_style)

    def _resize_browser(self) -> None:
        hwnd = self._browser_hwnd
        if not hwnd:
            return
        if not _is_window(hwnd):
            self._clear_attached_state()
            self._show_status("浏览器窗口已关闭。再次执行任务即可重新打开。")
            return
        size = self._host.size()
        # Chromium keeps a thin DWM frame on the left/right/bottom even after
        # WS_CAPTION/WS_THICKFRAME are removed. Place that frame just outside
        # the clipped host so only the actual browser surface is visible.
        user32 = _user32()
        self._enforce_hosted_window_style(hwnd, user32)
        client_rect = wintypes.RECT()
        if user32.GetClientRect(int(self._host.winId()), ctypes.byref(client_rect)):
            width = max(1, int(client_rect.right - client_rect.left))
            height = max(1, int(client_rect.bottom - client_rect.top))
        else:
            width = max(1, size.width())
            height = max(1, size.height())
        frame = max(1, int(round(8 * width / max(1, size.width()))))
        # Edge may restore its initial top-level dimensions shortly after
        # startup. SWP_FRAMECHANGED reapplies the hosted child frame while
        # setting the final slot dimensions in one operation.
        moved = user32.SetWindowPos(
            hwnd,
            0,
            -frame,
            0,
            width + frame * 2,
            height + frame,
            0x0074,  # SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW
        )
        if not moved:
            logger.warning(
                "Failed to resize hosted browser hwnd=%s host=%sx%s error=%s",
                hwnd,
                width,
                height,
                ctypes.get_last_error(),
            )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._resize_browser()

    def detach_browser(self) -> None:
        """Restore the browser as a normal top-level window on PawMate exit."""
        hwnd = self._browser_hwnd
        if not hwnd or not _is_window(hwnd):
            self._clear_attached_state()
            return
        try:
            user32 = _user32()
            user32.SetParent(hwnd, self._original_parent)
            if self._original_style:
                user32.SetWindowLongPtrW(hwnd, -16, self._original_style)
            if self._original_ex_style:
                user32.SetWindowLongPtrW(hwnd, -20, self._original_ex_style)
            user32.SetWindowPos(hwnd, 0, 80, 80, 1100, 760, 0x0027)
            user32.ShowWindow(hwnd, 5)
        except Exception as exc:
            logger.warning("Failed to restore hosted browser hwnd=%s: %s", hwnd, exc)
        finally:
            self._clear_attached_state()

    def _clear_attached_state(self) -> None:
        self._browser_hwnd = 0
        self._original_parent = 0
        self._original_style = 0
        self._original_ex_style = 0
        self._hosted_style = 0
        self._hosted_ex_style = 0


def request_embed_cdp_browser(cdp_url: str, visible: bool = True, process_id: int = 0) -> None:
    """Thread-safe entry point used by the Playwright runtime."""
    workspace = _workspace_ref() if _workspace_ref is not None else None
    if workspace is not None:
        workspace.embedRequested.emit(str(cdp_url or ""), bool(visible), max(0, int(process_id or 0)))


def _cdp_port(cdp_url: str) -> int:
    try:
        return int(urlparse(cdp_url).port or 0)
    except (TypeError, ValueError):
        return 0


def _find_browser_window(port: int, process_id: int = 0) -> int:
    if sys.platform != "win32" or (not port and not process_id):
        return 0
    browser_pids: set[int] = {int(process_id)} if process_id else set()
    try:
        import psutil

        if browser_pids:
            for pid in list(browser_pids):
                try:
                    process = psutil.Process(pid)
                    browser_pids.update(child.pid for child in process.children(recursive=True))
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    continue
        if port:
            token = f"--remote-debugging-port={port}"
            for process in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    name = str(process.info.get("name") or "").lower()
                    if not any(part in name for part in ("msedge", "chrome", "chromium")):
                        continue
                    command = " ".join(process.info.get("cmdline") or [])
                    if token not in command:
                        continue
                    browser_pids.add(int(process.info["pid"]))
                    browser_pids.update(child.pid for child in process.children(recursive=True))
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    continue
    except ImportError:
        pass
    if not browser_pids:
        return 0

    matches: list[int] = []
    user32 = _user32()

    @_WNDENUMPROC
    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in browser_pids or not user32.IsWindowVisible(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        if buffer.value.startswith("Chrome_WidgetWin"):
            matches.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(visit, 0)
    return matches[0] if matches else 0


def _is_window(hwnd: int) -> bool:
    return bool(hwnd and sys.platform == "win32" and _user32().IsWindow(hwnd))


def _user32():
    user32 = ctypes.windll.user32
    pointer = ctypes.c_void_p
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    return user32
