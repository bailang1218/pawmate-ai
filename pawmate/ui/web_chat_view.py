"""Web-based chat panel prototype for PawMate.

Startup-optimized: QWebEngineView creation is deferred to after
window.show() so the window frame appears immediately instead
of blocking on QtWebEngineProcess startup (~2-3s)."""
from __future__ import annotations

import sys
import logging
from pathlib import Path

from pawmate.qt_compat import (
    QLabel,
    QObject,
    QSizePolicy,
    QTimer,
    QUrl,
    QVBoxLayout,
    QWebChannel,
    QWidget,
    Qt,
    Signal,
)

from pawmate.app.conversation_manager import ConversationManager
from pawmate.ui.web_bridge import WebBridge


def _candidate_index_paths() -> list[Path]:
    """Return the most likely locations of the web entry HTML."""
    module_dir = Path(__file__).resolve().parent
    return [
        module_dir / "web" / "index.html",
        Path.cwd() / "pawmate" / "ui" / "web" / "index.html",
        Path.cwd() / "web" / "index.html",
    ]


def _resolve_index_html() -> Path | None:
    for candidate in _candidate_index_paths():
        if candidate.is_file():
            return candidate
    searched = "\n".join(f"  - {path}" for path in _candidate_index_paths())
    print(
        "[WebChatView] index.html not found. Searched paths:\n" + searched,
        file=sys.stderr,
    )
    return None


class WebChatView(QWidget):
    """Embedded WebEngine chat surface.

    Creates a lightweight placeholder immediately. The actual
    QWebEngineView is created ~100ms after widget visibility via
    QTimer, so the window frame shows without blocking on the
    QtWebEngineProcess startup.
    """

    load_ready = Signal()
    load_failed = Signal(str)

    def __init__(self, parent=None, mock_mode: bool = False,
                 window_bridge: QObject | None = None,
                 config_bridge: QObject | None = None):
        super().__init__(parent)
        log = logging.getLogger("pawmate")
        log.info("[WebChatView] mock_mode=%s", mock_mode)
        self.setObjectName("WebChatView")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.bridge = WebBridge(self, mock_mode=mock_mode)
        self._window_bridge = window_bridge
        self._config_bridge = config_bridge
        self._startup_error: str | None = None
        self._channel: QWebChannel | None = None
        self._web_view = None
        self._loading_label: QLabel | None = None
        self._init_called = False

        self._index_html = _resolve_index_html()

        # ── channel 对象注册缓冲队列（引擎就绪可能早于 QWebChannel 创建）──
        self._pending_channel_objects: list[tuple[str, QObject]] = []

        # Phase 1: show a fast loading label instead of blocking
        # on QtWebEngineProcess startup.
        self._init_fast()

    def _init_fast(self) -> None:
        """Fast phase: create layout + loading label, no QWebEngine."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if self._index_html is None:
            self._startup_error = "index.html not found"
            fallback = QLabel(
                "Web UI 未找到 index.html。\n"
                "请确认 pawmate/ui/web/index.html 已创建。",
                self,
            )
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fallback.setWordWrap(True)
            fallback.setStyleSheet(
                "QLabel { background: #f7fafc; color: #1f2d35; padding: 24px; }"
            )
            layout.addWidget(fallback)
            return

        self._loading_label = QLabel("Loading PawMate UI...", self)
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label.setStyleSheet(
            "QLabel { background: #f7fafc; color: #718096; font-size: 14px; }"
        )
        layout.addWidget(self._loading_label)

    def register_channel_object(self, name: str, obj: QObject) -> None:
        """Register a QObject on the existing QWebChannel."""
        log = logging.getLogger("pawmate")
        if self._channel is not None:
            self._channel.registerObject(name, obj)
            log.info("[WebChatView] registered channel object: %s", name)

    def showEvent(self, event):
        """Widget becomes visible — defer QWebEngine init to non-blocking timer."""
        super().showEvent(event)
        if not self._init_called and self._index_html is not None:
            self._init_called = True
            # Defer to after the event loop is running so the window
            # frame appears immediately. 100ms is enough for the
            # initial paint; the web view replaces the label when done.
            QTimer.singleShot(100, self._init_web_engine)

    def wire_conversation_manager(self, engine: "AgentEngine") -> None:
        """Engine ready callback: wire ConvManager to real engine."""
        log = logging.getLogger("pawmate")
        cm = getattr(self, "_conv_manager", None)
        if cm is not None:
            cm.set_engine(engine)
            log.info("[WebChatView] conversationManager wired to engine")
        else:
            log.warning("[WebChatView] _conv_manager not created yet, fallback")
            self._conv_manager = ConversationManager(engine)
            self.register_channel_object("conversationManager", self._conv_manager)

    def _init_web_engine(self) -> None:
        """Phase 2: create QWebEngineView (slow, ~2-3s) and replace label."""
        from pawmate.qt_compat import QColor, QWebEngineSettings, QWebEngineView

        log = logging.getLogger("pawmate")
        log.info("[WebChatView] Creating QWebEngineView...")

        layout = self.layout()
        if layout is None:
            return

        # Remove loading label
        if self._loading_label is not None:
            layout.removeWidget(self._loading_label)
            self._loading_label.deleteLater()
            self._loading_label = None

        self._web_view = QWebEngineView(self)
        self._web_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._web_view.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self._web_view.setAutoFillBackground(False)
        self._web_view.page().setBackgroundColor(QColor(0, 0, 0, 0))

        settings = self._web_view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )

        self._channel = QWebChannel(self._web_view.page())
        self._channel.registerObject("bridge", self.bridge)
        if self._window_bridge is not None:
            self._channel.registerObject("windowBridge", self._window_bridge)
        if self._config_bridge is not None:
            self._channel.registerObject("configBridge", self._config_bridge)
        # Register conversationManager placeholder (engine wires via set_engine later)
        self._conv_manager = ConversationManager()
        self._channel.registerObject("conversationManager", self._conv_manager)
        self._web_view.page().setWebChannel(self._channel)
        self._web_view.loadFinished.connect(self._on_load_finished)

        html_content = self._index_html.read_text("utf-8")
        base_url = QUrl.fromLocalFile(str(self._index_html.parent) + "/")
        self._web_view.setHtml(html_content, base_url)
        layout.addWidget(self._web_view)

        log.info("[WebChatView] QWebEngineView created and setHtml called")

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            message = "[WebChatView] Failed to load the Web UI page."
            print(message, file=sys.stderr)
            self._startup_error = "web page load failed"
            self.load_failed.emit(self._startup_error)
            return

        self._startup_error = None
        self.load_ready.emit()
        logging.getLogger("pawmate").info("[WebChatView] Web UI loaded")

    def is_web_runtime_available(self) -> bool:
        return self._startup_error is None


def create_demo_widget() -> WebChatView:
    """Convenience factory for standalone testing."""
    return WebChatView()
