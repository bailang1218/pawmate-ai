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
from pawmate.ui.app_icon import apply_app_icon
from pawmate.bridge.window_bridge import WindowBridge
from pawmate.bridge.config_bridge import ConfigBridge, set_config_path


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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._window_bridge = WindowBridge(self)
        set_config_path(Path(__file__).resolve().parent.parent / "config.json")
        self._config_bridge = ConfigBridge(self)
        self._window_bridge.closeRequested.connect(self.close)
        self._window_bridge.minimizeRequested.connect(self.showMinimized)
        self._window_bridge.maximizeRestoreRequested.connect(self._toggle_maximize_restore)

        central = QWidget()
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        central.setAutoFillBackground(False)
        self.setAutoFillBackground(False)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        self._web_chat_view = WebChatView(
            self, mock_mode=mock_mode,
            window_bridge=self._window_bridge,
            config_bridge=self._config_bridge,
        )
        layout.addWidget(self._web_chat_view)

        # State
        self._drag_active = False
        self._drag_start_global = QPoint()
        self._drag_start_frame = QPoint()
        self._resize_active = False
        self._resize_edge = None
        self._resize_start_global = QPoint()
        self._resize_start_geometry = QRect()

        # Install eventFilters and enable mouseTracking everywhere
        self._install_filters()
        # When QWebEngineView is created later (deferred init), re-install filters
        self._web_chat_view.load_ready.connect(self._install_filters)

    # ============================================================
    # Filter installation — everywhere, with mouseTracking
    # ============================================================
    def _install_filters(self):
        # Global QApplication filter (catches events before QWebEngineView)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # Self
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
                proxy.installEventFilter(self)
                proxy.setMouseTracking(True)

        # Every child widget — eventFilter only, no mouseTracking on all
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

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
        if x >= w - 430:
            return True
        return False

    def _dispatch_titlebar_web_button(self, x: int, y: int) -> bool:
        """Forward titlebar tool clicks that Qt may receive before Chromium."""
        w = self.width()
        if not (20 <= x <= 120 or self._is_sidebar_new_button_area(x, y) or x >= w - 430):
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
        if self.isMaximized(): self.showNormal()
        else: self.showMaximized()

    def is_web_ui_enabled(self) -> bool: return True
    def get_web_chat_view(self): return self._web_chat_view
    def get_web_bridge(self):
        if self._web_chat_view: return self._web_chat_view.bridge
        return None
    def get_config_bridge(self): return self._config_bridge

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
