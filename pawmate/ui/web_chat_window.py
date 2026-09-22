"""
WebChatWindow — frameless rounded window with drag+resize via global eventFilter.

Strategy:
- Global QApplication eventFilter catches mouse events BEFORE QWebEngineView/Chromium
- mouseTracking on all widgets enables cursor updates without pressing
- grabMouse() during drag/resize prevents Chromium from stealing events
- mapFromGlobal() for reliable window-local coordinate mapping
"""
from pathlib import Path
import json

from pawmate.qt_compat import (
    QApplication,
    QEvent,
    QMainWindow,
    QPoint,
    QRect,
    QVBoxLayout,
    QWidget,
    QTimer,
    Qt,
    Signal,
)

from pawmate.ui.web_chat_view import WebChatView
from pawmate.ui.embedded_browser import EmbeddedBrowserWorkspace
from pawmate.ui.app_icon import apply_app_icon
from pawmate.bridge.window_bridge import WindowBridge
from pawmate.bridge.config_bridge import ConfigBridge, set_config_path
from pawmate.bridge.browser_automation_bridge import BrowserAutomationBridge
from pawmate.bridge.cli_bridge import CliBridge


class WebChatWindow(QMainWindow):
    web_chat_failed = Signal(str)

    def __init__(self, mock_mode: bool = False):
        super().__init__()
        import logging
        logging.getLogger("pawmate").info("[WebChatWindow] mock_mode=%s", mock_mode)
        self.setWindowTitle("PawMate AI")
        apply_app_icon(self)
        self.resize(1200, 800)
        self.setMinimumSize(800, 560)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        # QtWebEngine is not reliable inside a translucent top-level window on
        # Windows (texture offsets, clipping and click-through). Keep the native
        # surface opaque until a dedicated native window shell is introduced.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self._window_bridge = WindowBridge(self)
        set_config_path(Path(__file__).resolve().parent.parent / "config.json")
        self._config_bridge = ConfigBridge(self)
        self._browser_automation_bridge = BrowserAutomationBridge(self)
        self._cli_bridge = CliBridge(self)
        self._window_bridge.closeRequested.connect(self.close)
        self._window_bridge.minimizeRequested.connect(self.showMinimized)
        self._window_bridge.maximizeRestoreRequested.connect(self._toggle_maximize_restore)

        central = QWidget()
        central.setObjectName("PawMateSurface")
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        central.setAutoFillBackground(True)
        central.setStyleSheet("#PawMateSurface { background-color: #f7fafc; }")
        self.setAutoFillBackground(True)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        self._web_chat_view = WebChatView(
            self, mock_mode=mock_mode,
            window_bridge=self._window_bridge,
            config_bridge=self._config_bridge,
            browser_automation_bridge=self._browser_automation_bridge,
            cli_bridge=self._cli_bridge,
        )
        layout.addWidget(self._web_chat_view)
        self._native_browser_workspace = EmbeddedBrowserWorkspace(central)
        self._native_browser_workspace.embedRequested.connect(self._show_browser_sidecar)
        self._browser_host_rect = QRect()
        self._browser_host_visible = False
        self._window_bridge.browserHostGeometryRequested.connect(self._set_browser_host_geometry)
        self._surface_recovery_needed = False
        self._surface_recovery_generation = 0

        # State
        self._drag_active = False
        self._drag_start_global = QPoint()
        self._drag_start_frame = QPoint()
        self._resize_active = False
        self._resize_edge = None
        self._resize_start_global = QPoint()
        self._resize_start_geometry = QRect()

        # Rounded geometry is a native input/paint mask, independent from web
        # transparency. This preserves both reliable hit-testing and corners.
        self._update_window_mask()

        # Install eventFilters and enable mouseTracking everywhere
        self._install_filters()
        # When QWebEngineView is created later (deferred init), re-install filters
        self._web_chat_view.load_ready.connect(self._install_filters)

    # ============================================================
    # Filter installation — everywhere, with mouseTracking
    # ============================================================
    def _install_filters(self):
        # Scope mouse filtering to this PawMate window. Application-wide
        # interception makes Chromium input pass through this handler multiple
        # times and can leave the whole frontend without clicks or focus.
        self.installEventFilter(self)
        self.setMouseTracking(True)

        # Central widget
        cw = self.centralWidget()
        if cw is not None:
            cw.installEventFilter(self)
            cw.setMouseTracking(True)

        # WebChatView
        if self._web_chat_view is not None:
            self._web_chat_view.installEventFilter(self)
            self._web_chat_view.setMouseTracking(True)

        # QWebEngineView and its children
        web_view = getattr(self._web_chat_view, "_web_view", None)
        if web_view is not None:
            web_view.installEventFilter(self)
            web_view.setMouseTracking(True)
            proxy = web_view.focusProxy()
            if proxy is not None:
                # This is the one Chromium child that receives native mouse
                # events. The filter returns False for page content/buttons and
                # consumes only titlebar drag or resize gestures.
                proxy.installEventFilter(self)
                proxy.setMouseTracking(True)

        # Every child widget — eventFilter only, no mouseTracking on all
        # Do not install on every child: WebEngine's internal input widgets
        # must receive native mouse events without a second interception.

    # ============================================================
    # eventFilter — safe, only mouse events, use mapFromGlobal
    # ============================================================
    def eventFilter(self, obj, event):
        etype = event.type()
        if etype not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                         QEvent.Type.MouseButtonRelease):
            return super().eventFilter(obj, event)
        if not self.isVisible():
            return super().eventFilter(obj, event)
        try:
            gpos = event.globalPosition().toPoint()
            # Early return: skip events far outside the window
            geom = self.frameGeometry().adjusted(-12, -12, 12, 12)
            if not geom.contains(gpos):
                return super().eventFilter(obj, event)
            local = self.mapFromGlobal(gpos)
            return self._handle_mouse(etype, event, gpos, local)
        except Exception:
            return False

    # ============================================================
    # Mouse handler
    # ============================================================
    def _handle_mouse(self, etype, event, gpos, local):
        if self.isMaximized():
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return False

        x, y = local.x(), local.y()
        w, h = self.width(), self.height()

        if etype == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return False

            # Resize edge
            edge = self._detect_edge(x, y, w, h)
            if edge:
                if self._start_system_resize(edge):
                    self._resize_active = False
                    self._resize_edge = None
                    return True
                self._resize_active = True
                self._resize_edge = edge
                self._resize_start_global = gpos
                self._resize_start_geometry = QRect(self.geometry())
                self.grabMouse()
                return True

            # Title bar: real web buttons stay clickable; every other titlebar pixel drags.
            if y <= 64:
                if self._dispatch_titlebar_web_button(x, y):
                    return True
                if self._is_titlebar_web_button_area(x, y):
                    return False
                if self._start_system_move():
                    self._drag_active = False
                    return True
                self._drag_active = True
                self._drag_start_global = gpos
                self._drag_start_frame = self.frameGeometry().topLeft()
                self.grabMouse()
                return True

            return False

        elif etype == QEvent.Type.MouseMove:
            if self._resize_active:
                delta = gpos - self._resize_start_global
                r = QRect(self._resize_start_geometry)
                e = self._resize_edge or ""
                if "right" in e:  r.setRight(max(r.right() + delta.x(), r.left() + self.minimumWidth()))
                if "bottom" in e: r.setBottom(max(r.bottom() + delta.y(), r.top() + self.minimumHeight()))
                if "left" in e:   r.setLeft(min(r.left() + delta.x(), r.right() - self.minimumWidth()))
                if "top" in e:    r.setTop(min(r.top() + delta.y(), r.bottom() - self.minimumHeight()))
                self.setGeometry(r)
                return True

            if self._drag_active:
                delta = gpos - self._drag_start_global
                self.move(self._drag_start_frame + delta)
                return True

            # Cursor hint
            edge = self._detect_edge(x, y, w, h)
            self._update_cursor(edge)
            return False

        elif etype == QEvent.Type.MouseButtonRelease:
            self._drag_active = False
            self._resize_active = False
            self._resize_edge = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            try:
                self.releaseMouse()
            except Exception:
                pass
            return False

        return False

    # ============================================================
    # Helpers
    # ============================================================
    def _start_system_move(self) -> bool:
        """Ask Qt/Windows to move the frameless window with native behavior."""
        try:
            handle = self.windowHandle()
            if handle is None or not hasattr(handle, "startSystemMove"):
                return False
            return bool(handle.startSystemMove())
        except Exception:
            return False

    def _start_system_resize(self, edge: str) -> bool:
        """Ask Qt/Windows to resize the frameless window with native behavior."""
        try:
            handle = self.windowHandle()
            if handle is None or not hasattr(handle, "startSystemResize"):
                return False
            edges = self._edge_to_qt_edges(edge)
            if edges is None:
                return False
            return bool(handle.startSystemResize(edges))
        except Exception:
            return False

    def _edge_to_qt_edges(self, edge: str):
        mapping = {
            "left": Qt.Edge.LeftEdge,
            "right": Qt.Edge.RightEdge,
            "top": Qt.Edge.TopEdge,
            "bottom": Qt.Edge.BottomEdge,
            "top_left": Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
            "top_right": Qt.Edge.TopEdge | Qt.Edge.RightEdge,
            "bottom_left": Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
            "bottom_right": Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
        }
        return mapping.get(edge)

    def _is_titlebar_web_button_area(self, x: int, y: int) -> bool:
        """Approximate only the visible titlebar buttons; the rest should drag."""
        w = self.width()
        if y < 8 or y > 44:
            return False
        # Left conversation-list button. Keep this forgiving: DPI/title padding
        # can shift the injected web button a bit, and misses become window drag.
        if 20 <= x <= 120:
            return True
        if self._is_sidebar_new_button_area(x, y):
            return True
        # Right tool/window controls. Keep this wider than the exact buttons for DPI safety.
        # The desktop-pet switch is wider than the icon buttons and sits before them.
        if x >= w - 150:
            return True
        return False

    def _dispatch_titlebar_web_button(self, x: int, y: int) -> bool:
        """Forward titlebar tool clicks that Qt may receive before Chromium."""
        w = self.width()
        if not (20 <= x <= 120 or self._is_sidebar_new_button_area(x, y) or x >= w - 150):
            return False
        web_view = getattr(self._web_chat_view, "_web_view", None)
        if web_view is None:
            return False

        # These ids are stable UI contracts in pawmate/ui/web/index.html.
        js = f"""
        (function() {{
          var sidebarNewHit = {str(self._is_sidebar_new_button_area(x, y)).lower()};
          if (sidebarNewHit && window.PawSidebar && window.PawSidebar.startBlankDraft) {{
            window.PawSidebar.startBlankDraft(function() {{
              if (window.PawSidebar.close) window.PawSidebar.close();
            }});
            return 'sidebarNewBtn';
          }}
          var el = document.elementFromPoint({int(x)}, {int(y)});
          var petToggle = el && el.closest && el.closest('#desktopPetTopToggle');
          if (petToggle) {{
            var petCheckbox = document.getElementById('desktopPetTopCheckbox');
            if (petCheckbox) petCheckbox.click();
            return 'desktopPetTopToggle';
          }}
          var btn = el && el.closest && el.closest('button');
          if (!btn) return '';
          var id = btn.id || '';
          if (id === 'sidebarToggle') {{
            if (window.PawSidebar && window.PawSidebar.toggle) window.PawSidebar.toggle();
            return id;
          }}
          if (id === 'sidebarNewBtn') {{
            if (window.PawSidebar && window.PawSidebar.startBlankDraft) {{
              window.PawSidebar.startBlankDraft(function() {{
                if (window.PawSidebar.close) window.PawSidebar.close();
              }});
            }} else {{
              btn.click();
            }}
            return id;
          }}
          if (id === 'skillsHallBtn') {{
            if (window.PawMateSkillsHall && window.PawMateSkillsHall.toggle) window.PawMateSkillsHall.toggle();
            return id;
          }}
          if (id === 'memoryHallBtn') {{
            if (window.PawMemoryHall && window.PawMemoryHall.toggle) window.PawMemoryHall.toggle();
            return id;
          }}
          if (id === 'settingsBtn') {{
            if (window.PawSettings && window.PawSettings.toggle) window.PawSettings.toggle();
            return id;
          }}
          if (id === 'closeSettingsBtn') {{
            if (window.PawSettings && window.PawSettings.close) window.PawSettings.close();
            else btn.click();
            return id;
          }}
          if (id === 'minimizeBtn' || id === 'maximizeBtn' || id === 'closeBtn') {{
            btn.click();
            return id;
          }}
          return '';
        }})();
        """
        try:
            web_view.page().runJavaScript(js)
            return True
        except Exception:
            return False

    @staticmethod
    def _is_sidebar_new_button_area(x: int, y: int) -> bool:
        """Approximate the sidebar '+' button when it overlaps the drag titlebar."""
        return 226 <= x <= 280 and 8 <= y <= 54

    def _detect_edge(self, x, y, w, h):
        m = 12
        if x <= m and y <= m:          return "top_left"
        if x >= w-m and y <= m:        return "top_right"
        if x <= m and y >= h-m:        return "bottom_left"
        if x >= w-m and y >= h-m:      return "bottom_right"
        if x <= m:                     return "left"
        if x >= w-m:                   return "right"
        if y <= m:                     return "top"
        if y >= h-m:                   return "bottom"
        return None

    def _update_cursor(self, edge):
        c = {
            "left":         Qt.CursorShape.SizeHorCursor,
            "right":        Qt.CursorShape.SizeHorCursor,
            "top":          Qt.CursorShape.SizeVerCursor,
            "bottom":       Qt.CursorShape.SizeVerCursor,
            "top_left":     Qt.CursorShape.SizeFDiagCursor,
            "bottom_right": Qt.CursorShape.SizeFDiagCursor,
            "top_right":    Qt.CursorShape.SizeBDiagCursor,
            "bottom_left":  Qt.CursorShape.SizeBDiagCursor,
        }.get(edge, Qt.CursorShape.ArrowCursor)
        self.setCursor(c)

    # ============================================================
    # Window control + accessors
    # ============================================================
    def _toggle_maximize_restore(self):
        if self.isMaximized():
            self.showNormal()
            QTimer.singleShot(0, self._update_window_mask)
        else:
            self.clearMask()
            self.showMaximized()

    def is_web_ui_enabled(self) -> bool: return True
    def get_web_chat_view(self): return self._web_chat_view
    def get_web_bridge(self):
        if self._web_chat_view: return self._web_chat_view.bridge
        return None
    def get_config_bridge(self): return self._config_bridge
    def get_browser_automation_bridge(self): return self._browser_automation_bridge
    def get_cli_bridge(self): return self._cli_bridge

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_window_mask()
        self._apply_browser_host_geometry()

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        self._schedule_surface_recovery(remount=self._surface_recovery_needed)

    def changeEvent(self, event):  # noqa: N802 - Qt API
        super().changeEvent(event)
        event_type = event.type()
        if event_type == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._surface_recovery_needed = True
                host = getattr(self, "_native_browser_workspace", None)
                if host is not None:
                    host.set_workspace_visible(False)
                return
            self._schedule_surface_recovery(remount=True)
            return
        if event_type == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._schedule_surface_recovery(
                    remount=self._surface_recovery_needed
                )
            else:
                self._surface_recovery_needed = True

    def _schedule_surface_recovery(self, remount: bool = False) -> None:
        if not hasattr(self, "_surface_recovery_generation"):
            return
        self._surface_recovery_generation += 1
        generation = self._surface_recovery_generation
        QTimer.singleShot(
            0,
            lambda: self._recover_window_surface(generation, remount),
        )
        QTimer.singleShot(
            80,
            lambda: self._recover_window_surface(generation, False),
        )

    def _recover_window_surface(self, generation: int, remount: bool) -> None:
        if (
            generation != self._surface_recovery_generation
            or not self.isVisible()
            or self.isMinimized()
        ):
            return
        self._surface_recovery_needed = False
        self._update_window_mask()
        web_chat_view = getattr(self, "_web_chat_view", None)
        if web_chat_view is not None:
            web_chat_view.show()
            recover = getattr(web_chat_view, "recover_render_surface", None)
            if callable(recover):
                recover(remount=remount)
        central = self.centralWidget()
        if central is not None:
            central.update()
        self.update()
        self._apply_browser_host_geometry()

    def closeEvent(self, event):  # noqa: N802 - Qt API
        self._native_browser_workspace.detach_browser()
        self._cli_bridge.shutdown()
        super().closeEvent(event)

    def _set_browser_host_geometry(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        visible: bool,
    ) -> None:
        self._browser_host_rect = QRect(int(x), int(y), int(width), int(height))
        self._browser_host_visible = bool(visible and width > 0 and height > 0)
        self._apply_browser_host_geometry()

    def _show_browser_sidecar(self, _cdp_url: str, visible: bool, _process_id: int) -> None:
        """Ask the web shell for a current slot before showing the native host."""
        if not visible:
            return
        web_view = getattr(self._web_chat_view, "_web_view", None)
        if web_view is None:
            return
        web_view.page().runJavaScript(
            """
            (function revealBrowserSidecar(attempt) {
              if (window.PawWorkspace &&
                  typeof window.PawWorkspace.showBrowserSidecar === "function") {
                window.PawWorkspace.showBrowserSidecar();
                return;
              }
              if (attempt < 20) {
                setTimeout(function () { revealBrowserSidecar(attempt + 1); }, 100);
              }
            })(0);
            """
        )

    def _apply_browser_host_geometry(self) -> None:
        host = getattr(self, "_native_browser_workspace", None)
        central = self.centralWidget()
        if host is None or central is None or not self._browser_host_visible:
            if host is not None:
                host.set_workspace_visible(False)
            return
        bounds = central.rect()
        rect = self._browser_host_rect.intersected(bounds)
        if rect.width() < 80 or rect.height() < 80:
            host.set_workspace_visible(False)
            return
        host.setGeometry(rect)
        host.set_workspace_visible(True)

    def _update_window_mask(self) -> None:
        """Clip only the native outer corners; keep WebEngine fully opaque."""
        import sys
        if sys.platform != "win32":
            self.clearMask()
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            if self.isMaximized():
                user32.SetWindowRgn(hwnd, 0, True)
                return

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return
            width = max(1, rect.right - rect.left)
            height = max(1, rect.bottom - rect.top)
            # CSS uses border-radius:18px. GDI expects the ellipse diameter in
            # physical pixels; account for Qt/Windows display scaling.
            diameter = max(2, int(round(36 * float(self.devicePixelRatioF()))))
            region = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, width + 1, height + 1, diameter, diameter
            )
            if region:
                # On success Windows owns the HRGN; do not DeleteObject it.
                if not user32.SetWindowRgn(hwnd, region, True):
                    ctypes.windll.gdi32.DeleteObject(region)
        except Exception:
            # Keep a usable rectangular window if native clipping is absent.
            self.clearMask()

    def open_settings_section(self, section: str = "general", message: str | None = None) -> bool:
        def _run() -> bool:
            web_view = getattr(self._web_chat_view, "_web_view", None)
            if web_view is None:
                return False
            js = f"""
            (function() {{
              var section = {json.dumps(section)};
              var message = {json.dumps(message)};
              function openNow() {{
                if (!window.PawSettings || typeof window.PawSettings.openSection !== "function") {{
                  return false;
                }}
                window.PawSettings.openSection(section, message);
                return true;
              }}
              if (!openNow()) setTimeout(openNow, 300);
            }})();
            """
            try:
                web_view.page().runJavaScript(js)
                return True
            except Exception:
                return False

        if _run():
            return True

        try:
            self._web_chat_view.load_ready.connect(lambda: QTimer.singleShot(250, _run))
        except Exception:
            return False
