"""PawMate AI 主入口。"""
import asyncio
import importlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

from pawmate.qt_compat import QApplication, Qt, QTimer

import pawmate.config as config
from pawmate.bridge.contracts import ErrorEvent, ProgressUpdateEvent
from pawmate.bridge.event_bus import event_bus
from pawmate.bridge.worker import AgentWorker
from pawmate.bridge.ws_server import WsChatServer
from pawmate.core.engine import AgentEngine
from pawmate.core.environment_diagnostics import log_startup_environment_diagnostics
from pawmate.core.llm_router import get_available_llm_providers, resolve_llm_route
from pawmate.core.model_catalog import (
    build_default_llm_config,
    normalize_llm_config_keys,
    normalize_provider_key,
)
from pawmate.core.redaction import redact_text
from pawmate.storage.history_store import HistoryStore
from pawmate.ui.app_icon import apply_app_icon
from pawmate.ui.native_style import apply_native_style

# ============================================================================
# 抑制 Python 3.14 ProactorEventLoop 子进程 cleanup 已知警告
import warnings
warnings.filterwarnings('ignore', message='unclosed transport', category=ResourceWarning)
warnings.filterwarnings('ignore', message='I/O operation on closed pipe', category=ResourceWarning)

# 日志系统 —— 拦截 Python logging 并发往嵌入式终端
# ============================================================================

class _LogBuffer:
    """启动初期缓存日志，终端就绪后回放。"""
    def __init__(self):
        self._buffer: list[tuple[str, str]] = []
        self._ready = False
        self._handler = None

    def set_handler(self, handler):
        self._handler = handler

    def write(self, text: str, level: str = "INFO"):
        if self._ready and self._handler:
            # 通过 QTimer 确保 terminal.append_log 在主线程执行
            from pawmate.qt_compat import QTimer
            QTimer.singleShot(0, lambda t=text, l=level: self._handler(t, l))
        else:
            self._buffer.append((text, level))

    def flush(self):
        if self._ready and self._handler and self._buffer:
            from pawmate.qt_compat import QTimer
            for text, level in self._buffer:
                QTimer.singleShot(0, lambda t=text, l=level: self._handler(t, l))
            self._buffer.clear()

    def set_ready(self):
        self._ready = True
        self.flush()


log_buffer = _LogBuffer()


def setup_logging():
    """配置 Python root logger 捕获所有日志。"""
    logger = logging.getLogger("pawmate")
    logger.setLevel(logging.INFO)

    class _TerminalHandler(logging.Handler):
        def emit(self, record):
            level_map = {
                logging.INFO: "INFO",
                logging.WARNING: "WARN",
                logging.ERROR: "ERROR",
                logging.DEBUG: "DEBUG",
                logging.CRITICAL: "ERROR",
            }
            level = level_map.get(record.levelno, "INFO")
            log_buffer.write(record.getMessage(), level)

    if not any(getattr(h, "_pawmate_terminal_handler", False) for h in logger.handlers):
        terminal_handler = _TerminalHandler()
        terminal_handler._pawmate_terminal_handler = True
        logger.addHandler(terminal_handler)

    if not any(getattr(h, "_pawmate_file_handler", False) for h in logger.handlers):
        try:
            from pawmate.core.app_logs import get_log_path

            file_handler = RotatingFileHandler(
                get_log_path(),
                maxBytes=2 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler._pawmate_file_handler = True
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
            logger.addHandler(file_handler)
        except Exception:
            pass
    return logger


pawmate_logger = setup_logging()


def _create_desktop_pet_integration(bridge: Any, chat_service: Any, parent: Any) -> Optional[Any]:
    try:
        module = importlib.import_module("pawmate.desktop_pet.integration")
        integration_class = getattr(module, "DesktopPetIntegration")
    except Exception as exc:
        pawmate_logger.info("[DesktopPet] integration unavailable; continuing without desktop pet: %s", exc)
        return None

    try:
        return integration_class(
            bridge=bridge,
            chat_service=chat_service,
            parent=parent,
        )
    except Exception as exc:
        pawmate_logger.warning("[DesktopPet] integration disabled after startup failure: %s", exc, exc_info=True)
        return None

# ============================================================================
# stdout/stderr 重定向 —— 捕获所有 print() 到终端日志
# ============================================================================
class _StdoutRedirector:
    def __init__(self, original_stream, level: str):
        self._original = original_stream
        self._level = level

    def write(self, text):
        if text.strip():
            log_buffer.write(text.strip(), self._level)
        if self._original is not None:
            try:
                self._original.write(text)
            except Exception:
                pass

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self):
        if self._original is not None and hasattr(self._original, "isatty"):
            try:
                return bool(self._original.isatty())
            except Exception:
                return False
        return False


def redirect_stdio():
    """重定向 stdout/stderr 到日志系统。"""
    sys.stdout = _StdoutRedirector(sys.stdout, "INFO")
    sys.stderr = _StdoutRedirector(sys.stderr, "ERROR")


CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "llm": build_default_llm_config(),
    "ui": {
        "ui_mode": "web",
        "web_ui_mock_mode": False,
        "language_mode": "zh-CN",
    },
    "tools": {
        "browser_use": {
            "enabled": True,
            "browser": "edge",
            "headless": False,
            "timeout": 180,
            "attach_mode": "auto",
            "cdp_url": "",
            "profile_directory": "auto",
            "automation_level": "standard",
        }
    },
    "runtime": {
        "task_timeout_seconds": 0,
    },
    "security": {
        "sandbox_mode": "safe",
        "path_access_mode": "strict",
        "allowed_roots": ["~"],
        "command_sandbox_mode": "safe",
        "mode": "strict",
        "allowed_path": ["~"],
        "qwebengine_disable_sandbox": False,
    },
    "websocket": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": config.WS_PORT,
        "auth_token": "",
    },
    "approval": {
        "llm_explain_enabled": True,
        "auto_approve_tools": False,
    },
    "assistant": {
        "language_mode": "auto",
    },
}


def _is_placeholder(value: str) -> bool:
    return not value or "placeholder" in value.lower() or "edit-me" in value.lower()


def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_config_file() -> Dict[str, Any]:
    """确保配置文件存在且结构完整。"""
    current: Dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                current = json.load(f)
        except Exception:
            current = {}

    merged = _merge_dict(DEFAULT_CONFIG, current)
    if isinstance(merged.get("llm"), dict):
        merged["llm"] = normalize_llm_config_keys(merged["llm"])
    # Strip whitespace from provider value
    if "provider" in merged.get("llm", {}):
        merged["llm"]["provider"] = normalize_provider_key(str(merged["llm"]["provider"]))
    # Remove duplicate llm keys (corrupted by newline-prefixed saves)
    llm_keys = list(merged.get("llm", {}).keys())
    for k in llm_keys:
        sk = k.strip()
        if k != sk and sk in merged["llm"]:
            del merged["llm"][k]
    approval_cfg = merged.get("approval")
    if isinstance(approval_cfg, dict):
        approval_cfg.pop("disable_confirmations", None)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return merged


def has_valid_api_key(config_data: Dict[str, Any]) -> bool:
    """检查是否已经配置可用的 API Key。"""
    llm_cfg = config_data.get("llm", {})
    llm_mode = str(llm_cfg.get("mode", "")).strip().lower() if isinstance(llm_cfg, dict) else ""
    if llm_mode in {"auto", "router", "route"}:
        return bool(get_available_llm_providers(config_data))
    provider = normalize_provider_key(str(llm_cfg.get("provider", "deepseek")))

    env_key = f"{provider.upper()}_API_KEY"
    env_val = os.getenv(env_key, "").strip()
    if env_val and not _is_placeholder(env_val):
        return True

    provider_cfg = llm_cfg.get(provider, {})
    cfg_key = str(provider_cfg.get("api_key", "")).strip()
    return bool(cfg_key and not _is_placeholder(cfg_key))


def _llm_runtime_signature(config_data: Dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Return the config slice that requires rebuilding the active LLM client."""
    llm_cfg = config_data.get("llm", {})
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    provider = normalize_provider_key(str(llm_cfg.get("provider", "deepseek")))
    llm_mode = str(llm_cfg.get("mode", "")).strip().lower()
    if llm_mode in {"auto", "router", "route"}:
        provider_cfg = {
            key: value
            for key, value in llm_cfg.items()
            if key != "provider"
        }
    else:
        provider_cfg = llm_cfg.get(provider, {})
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}
    serialized = tuple(
        sorted(
            (str(key), json.dumps(value, sort_keys=True, ensure_ascii=False))
            for key, value in provider_cfg.items()
        )
    )
    return provider, serialized


def _llm_runtime_label(config_data: Dict[str, Any]) -> str:
    llm_cfg = config_data.get("llm", {})
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    provider = normalize_provider_key(str(llm_cfg.get("provider", "deepseek")))
    provider_cfg = llm_cfg.get(provider, {})
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}
    model = str(provider_cfg.get("model", "")).strip() or "(default)"
    return f"{provider}/{model}"


class PawMateApp:
    """应用主类。"""

    def __init__(self):
        self._app = QApplication.instance() or QApplication(sys.argv)
        apply_app_icon()
        apply_native_style(self._app)
        self._worker: Optional[AgentWorker] = None
        self._engine: Optional[AgentEngine] = None
        self._ws_server: Optional[Any] = None
        self._desktop_pet: Optional[Any] = None
        self._cfg: Dict[str, Any] = {}
        self._setup_required = False
        self._engine_init_in_progress = False
        self._pending_engine_restart = False

        # 先读取配置，但不阻塞 mock 模式
        cfg = ensure_config_file()
        ui_cfg = cfg.get("ui", {})
        is_mock = ui_cfg.get("web_ui_mock_mode", False)

        # API Key 检查（mock 模式跳过，不弹配置框）
        self._setup_required = not is_mock and not has_valid_api_key(cfg)
        if self._setup_required:
            pawmate_logger.info("[Setup] LLM credentials missing; opening Web settings after startup")
        self._cfg = cfg

        self._history_store = HistoryStore()

        # ============================================================
        # 窗口创建：通过 window_factory 统一路由
        # ============================================================
        from pawmate.app.window_factory import create_main_window

        self._main_window = create_main_window(self._cfg)

        # ============================================================
        # ChatService — 统一聊天后端入口
        # ============================================================
        from pawmate.core.chat_service import ChatService

        chat_cfg = dict(self._cfg)
        chat_runtime = dict(chat_cfg.get("runtime", {}))
        chat_runtime["queue_until_worker_ready"] = True
        chat_cfg["runtime"] = chat_runtime
        self._chat_service = ChatService(chat_cfg)

        # 设置 Web UI 信号连接
        bridge = self._main_window.get_web_bridge()
        if bridge is not None:
            from pawmate.app.wiring import wire_web_chat_bridge

            wire_web_chat_bridge(self, bridge, self._chat_service)
            self._desktop_pet = _create_desktop_pet_integration(
                bridge=bridge,
                chat_service=self._chat_service,
                parent=self._main_window,
            )
            self._sync_desktop_pet_from_config(self._cfg)
            config_bridge = getattr(self._main_window, "get_config_bridge", lambda: None)()
            if config_bridge is not None:
                config_bridge.configSaved.connect(self._on_config_saved)

        self._connect_signals()
        self._main_window.web_chat_failed.connect(self._on_web_chat_failed)
        self._app.aboutToQuit.connect(self.quit)

    def run(self) -> int:
        # 重定向 print() 到终端日志
        redirect_stdio()

        # 显示窗口并激活
        self._main_window.show()
        self._main_window.raise_()
        self._main_window.activateWindow()
        if self._setup_required:
            pawmate_logger.info("[Setup] waiting for LLM configuration before engine startup")
            self._open_initial_setup()
            return self._app.exec()

        pawmate_logger.info("窗口已显示")
        pawmate_logger.info("后台启动引擎初始化...")

        # 后台线程创建引擎（带进度反馈）
        result = {"engine": None, "error": None}

        def _init():
            try:
                event_bus.publish(ProgressUpdateEvent(0, 5, "准备初始化..."))
                event_bus.publish(ProgressUpdateEvent(1, 5, "加载配置..."))
                event_bus.publish(ProgressUpdateEvent(2, 5, "检测运行环境..."))
                log_startup_environment_diagnostics(self._cfg)
                pawmate_logger.info("开始创建引擎（含 MCP 工具加载）...")

                eng = self._create_engine_sync(self._cfg)

                if eng is None:
                    result["error"] = "引擎初始化失败：未返回引擎实例"
                    return

                event_bus.publish(ProgressUpdateEvent(4, 5, "引擎构建完成"))
                result["engine"] = eng
                pawmate_logger.info("引擎创建成功")
            except Exception as e:
                pawmate_logger.error(f"引擎初始化异常: {e}")
                result["error"] = str(e)

        threading.Thread(target=_init, daemon=True).start()

        # 轮询检查引擎是否就绪（每 500ms 查一次，不阻塞 UI）
        def _poll():
            if result["error"]:
                pawmate_logger.error(f"引擎初始化失败: {result['error']}")
                self._on_engine_error(result["error"])
                return
            if result["engine"] is not None:
                event_bus.publish(ProgressUpdateEvent(5, 5, "完成！"))
                self._on_engine_ready(result["engine"])
                return
            QTimer.singleShot(100, _poll)

        QTimer.singleShot(50, _poll)
        return self._app.exec()

    def _open_initial_setup(self) -> None:
        message = "请先配置模型服务，再开始使用 PawMate。"

        def _open() -> None:
            opener = getattr(self._main_window, "open_settings_section", None)
            if callable(opener):
                opener("llm", message)
            else:
                pawmate_logger.warning("[Setup] Web settings opener unavailable")

        QTimer.singleShot(600, _open)

    def _start_engine_initialization(self) -> None:
        if self._engine is not None or self._engine_init_in_progress:
            return
        self._engine_init_in_progress = True
        pawmate_logger.info("[Engine] starting after configuration save")
        result = {"engine": None, "error": None}

        def _init():
            try:
                event_bus.publish(ProgressUpdateEvent(0, 5, "准备初始化..."))
                event_bus.publish(ProgressUpdateEvent(1, 5, "加载配置..."))
                event_bus.publish(ProgressUpdateEvent(2, 5, "检测运行环境..."))
                log_startup_environment_diagnostics(self._cfg)
                eng = self._create_engine_sync(self._cfg)
                if eng is None:
                    result["error"] = "引擎初始化失败：未返回引擎实例"
                    return
                event_bus.publish(ProgressUpdateEvent(4, 5, "引擎构建完成"))
                result["engine"] = eng
            except Exception as e:
                result["error"] = str(e)

        threading.Thread(target=_init, daemon=True).start()

        def _poll():
            if result["error"]:
                self._engine_init_in_progress = False
                self._on_engine_error(result["error"])
                return
            if result["engine"] is not None:
                self._engine_init_in_progress = False
                event_bus.publish(ProgressUpdateEvent(5, 5, "完成"))
                self._on_engine_ready(result["engine"])
                return
            QTimer.singleShot(100, _poll)

        QTimer.singleShot(50, _poll)

    def _create_engine_sync(self, cfg: Dict[str, Any]):
        """同步创建引擎（在后台线程中调用）。
        手动 loop，跳过 shutdown_default_executor/shutdown_asyncgens。"""
        import asyncio

        llm_cfg = cfg.get("llm", {})
        try:
            route = resolve_llm_route("chat", cfg)
            provider = route.provider
            model = route.model
        except Exception:
            provider = str(llm_cfg.get("provider", "deepseek")).strip().lower()
            model = str(llm_cfg.get(provider, {}).get("model", "")).strip() or None

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            engine = loop.run_until_complete(
                AgentEngine.create(
                    history_store=self._history_store,
                    app_config=cfg,
                    provider=provider,
                    model=model,
                )
            )
            return engine
        except Exception as e:
            pawmate_logger.error(f"引擎创建失败: {e}")
            return None
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def _on_tool_confirm_resolved(self, allowed: bool) -> None:
        """用户通过 Web UI 确认/拒绝了工具调用。"""
        if self._engine is not None:
            self._engine.resolve_confirm(allowed)

    def _on_config_saved(self, _message: str) -> None:
        old_signature = _llm_runtime_signature(self._cfg)
        cfg = ensure_config_file()
        new_signature = _llm_runtime_signature(cfg)
        self._cfg = cfg
        self._sync_desktop_pet_from_config(cfg)
        if self._engine is not None and hasattr(self._engine, "apply_approval_config"):
            self._engine.apply_approval_config(cfg)
        if self._setup_required and has_valid_api_key(cfg):
            self._setup_required = False
            self._start_engine_initialization()
            return
        if self._engine is not None and old_signature != new_signature:
            if not has_valid_api_key(cfg):
                pawmate_logger.warning(
                    "[Config] LLM config changed to %s but API key is missing; engine restart skipped",
                    _llm_runtime_label(cfg),
                )
                return
            self._pending_engine_restart = True
            self._restart_engine_after_config_change()

    def _restart_engine_after_config_change(self) -> None:
        if not self._pending_engine_restart:
            return
        if self._engine_init_in_progress:
            QTimer.singleShot(500, self._restart_engine_after_config_change)
            return
        if hasattr(self, "_chat_service") and self._chat_service.is_busy():
            pawmate_logger.info("[Config] delaying LLM restart until current turn finishes")
            QTimer.singleShot(1000, self._restart_engine_after_config_change)
            return

        self._pending_engine_restart = False
        pawmate_logger.info("[Config] restarting engine with LLM %s", _llm_runtime_label(self._cfg))
        self._stop_runtime_engine()
        self._start_engine_initialization()

    def _stop_runtime_engine(self) -> None:
        if self._ws_server is not None:
            try:
                self._ws_server.stop()
            except Exception as exc:
                pawmate_logger.warning("[WebSocket] stop before engine restart failed: %s", exc)
            self._ws_server = None
        if self._worker is not None:
            try:
                self._worker.stop()
            except Exception as exc:
                pawmate_logger.warning("[Engine] worker stop before restart failed: %s", exc)
            self._worker = None
        self._engine = None

    def _sync_desktop_pet_from_config(self, cfg: Dict[str, Any]) -> None:
        if self._desktop_pet is None:
            return
        desktop_pet = cfg.get("desktop_pet", {}) if isinstance(cfg.get("desktop_pet", {}), dict) else {}
        if hasattr(self._desktop_pet, "set_reply_surface"):
            self._desktop_pet.set_reply_surface(str(desktop_pet.get("reply_surface", "window")))
        enabled = bool(desktop_pet.get("enabled", False))
        self._desktop_pet.set_enabled(enabled)

    def _on_engine_ready(self, engine):
        """引擎初始化完成回调"""
        self._engine = engine
        self._worker = AgentWorker(engine)
        self._worker.start()

        # 将 worker 注册到 ChatService（real 模式需要）
        if hasattr(self, '_chat_service'):
            self._chat_service.set_worker(self._worker)

        # ── 多对话管理器（wire 到已注册的占位对象）───────
        try:
            view = self._main_window.get_web_chat_view()
            if view is not None:
                view.wire_conversation_manager(engine)
                pawmate_logger.info("[ConversationManager] wired to engine")

            # 对话结束后自动根据首条消息生成标题
            if hasattr(self, '_chat_service'):
                from pawmate.app.wiring import _connect_once

                _connect_once(self._chat_service.finished, self._on_auto_title)
                pawmate_logger.info("[ConversationManager] 已连接自动标题")
        except Exception as e:
            pawmate_logger.warning("[ConversationManager] wire 失败: %s", e)

        # 启动 WebSocket 服务器
        try:
            ws_cfg = self._cfg.get("websocket", {})
            if isinstance(ws_cfg, dict) and ws_cfg.get("enabled", False):
                ws_token = (
                    os.getenv("PAWMATE_WS_TOKEN", "").strip()
                    or str(ws_cfg.get("auth_token", "")).strip()
                )
                if not ws_token:
                    pawmate_logger.warning(
                        "[WebSocket] disabled: websocket.enabled=true requires auth_token "
                        "or PAWMATE_WS_TOKEN"
                    )
                else:
                    ws_host = str(ws_cfg.get("host", "127.0.0.1") or "127.0.0.1")
                    ws_port = int(ws_cfg.get("port", config.WS_PORT) or config.WS_PORT)
                    self._ws_server = WsChatServer(
                        self._worker,
                        host=ws_host,
                        port=ws_port,
                        auth_token=ws_token,
                    )
                    self._ws_server.connect_bridge()
                    self._ws_server.start()
                    pawmate_logger.info(
                        "WebSocket server started at ws://%s:%s (auth required)",
                        ws_host,
                        ws_port,
                    )
            else:
                pawmate_logger.info("[WebSocket] disabled by config")
        except Exception as e:
            pawmate_logger.warning(f"WebSocket 服务器启动失败: {e}")

        try:
            tools = engine._registry.tool_count()
            pawmate_logger.info(f"{tools} 个工具加载完毕")
        except Exception:
            pass





    def _relay_tool_event_to_web(self, payload: dict) -> None:
        """ChatService.tool_event → WebBridge.appendToolCard 路由"""
        bridge = self._main_window.get_web_bridge()
        if bridge is None:
            return
        evt_type = payload.get("type", "")
        name = payload.get("name", "")
        detail = redact_text(payload.get("detail", ""))
        duration = payload.get("duration", "")
        status_map = {"start": "running", "done": "completed", "error": "error"}
        status = status_map.get(evt_type, "completed")
        bridge.appendToolCard.emit(name, detail, status, duration)

    def _on_web_chat_failed(self, error_msg: str) -> None:
        """WebChatView 加载失败时记录日志。"""
        pawmate_logger.warning("WebChatView 加载失败: %s", error_msg)

    def _on_auto_title(self) -> None:
        """对话结束后自动根据首条消息生成会话标题。"""
        try:
            view = self._main_window.get_web_chat_view()
            cm = getattr(view, '_conv_manager', None) if view else None
            if cm is None:
                return
            sid = cm.get_current_session_id()
            if sid:
                title = cm.auto_title_from_first_message(sid)
                if title:
                    pawmate_logger.info("[ConversationManager] 自动标题: %s", title)
        except Exception as e:
            pawmate_logger.warning("[ConversationManager] 自动标题失败: %s", e)

    def _on_engine_error(self, error_msg: str):
        """引擎初始化失败回调"""
        pawmate_logger.error("引擎初始化失败: %s", error_msg)





        event_bus.publish(ErrorEvent(f"引擎初始化失败: {error_msg}"))

    def _connect_signals(self) -> None:
        """Web UI 模式：ChatService 接管所有信号，无额外连接。"""
        pass

    @staticmethod
    def _build_system_prompt(cfg: dict) -> str:
        """Build system prompt with language mode from config.

        Note: still kept for manual usage;
        for new code prefer AgentEngine.create(app_config=cfg).
        """
        try:
            from pawmate.core.prompt_assembler import build_prompt_from_config
            from pawmate.tools.registry import ToolRegistry
            from pawmate.tools.builtin_gateway import register_builtin_tools
            reg = ToolRegistry()
            register_builtin_tools(reg)
            return build_prompt_from_config(cfg, reg)
        except Exception:
            return config.DEFAULT_SYSTEM_PROMPT

    def quit(self) -> None:
        if self._desktop_pet is not None:
            self._desktop_pet.stop()
        if self._ws_server is not None:
            try:
                self._ws_server.stop()
            except Exception:
                pass
        if self._worker is not None:
            self._worker.stop()


def main() -> int:
    # QtWebEngine 兼容性：QApplication 创建前设置
    try:
        startup_cfg = ensure_config_file()
        sec_cfg = startup_cfg.get("security", {})
        cfg_disable_sandbox = (
            isinstance(sec_cfg, dict)
            and bool(sec_cfg.get("qwebengine_disable_sandbox", False))
        )
    except Exception:
        cfg_disable_sandbox = False
    disable_sandbox = (
        os.getenv("PAWMATE_QWEBENGINE_DISABLE_SANDBOX", "").strip() == "1"
        or cfg_disable_sandbox
    )
    if disable_sandbox:
        os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    extra = "--no-sandbox" if disable_sandbox else ""
    # GPU 默认不禁用（动画性能更好）。遇崩溃时设置 QTWEBENGINE_DISABLE_GPU=1
    if os.environ.get("QTWEBENGINE_DISABLE_GPU") == "1":
        extra = f"{extra} --disable-gpu --disable-gpu-compositing".strip()
    if extra and extra not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} {extra}".strip()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = PawMateApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
