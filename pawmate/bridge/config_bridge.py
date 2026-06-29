"""QWebChannel bridge for reading/saving config.json from the Web UI."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from pawmate.app.conversation_repository_factory import (
    create_conversation_repository_for_memory,
)
from pawmate.app.memory_settings_service import (
    MemorySettingsService,
    default_memory_status,
    initializing_items,
)
from pawmate.qt_compat import QApplication, QMessageBox, QObject, Signal, Slot

logger = logging.getLogger("pawmate")

# Path to config.json (set by caller if different)
_CONFIG_PATH: Path = Path(__file__).resolve().parent.parent / "config.json"
_TRANSIENT_CONFIG_KEYS = {"_memory_status"}
_CONFIRM_BYPASS_ACTIONS = {"save_config"}

_ACTION_LABELS = {
    "clear_app_logs": "清空应用日志",
    "update_episodic_memory": "修改一条长期记忆",
    "delete_episodic_memory": "删除一条长期记忆",
    "create_core_memory": "新增核心记忆",
    "update_core_memory": "修改核心记忆",
    "delete_core_memory": "删除核心记忆",
    "toggle_pin_core_memory": "切换核心记忆置顶状态",
}

_memory_settings_service: MemorySettingsService | None = None
_bridge_instance = None


def inject_engine(engine, bridge=None) -> None:
    """Called after engine.create() to enable memory settings queries."""
    ltm = engine.long_term_memory
    conversation_repository = create_conversation_repository_for_memory(ltm)
    inject_memory_settings_service(
        MemorySettingsService(
            ltm,
            conversation_repository=conversation_repository,
        ),
        bridge=bridge,
    )


def inject_memory_settings_service(
    service: MemorySettingsService | None,
    bridge=None,
) -> None:
    """Called after engine memory initialization to enable settings memory slots."""
    global _memory_settings_service, _bridge_instance
    _memory_settings_service = service
    if bridge is not None:
        _bridge_instance = bridge
        try:
            bridge.memoryReadyChanged.emit()
        except Exception:
            pass


def set_config_path(path: Path) -> None:
    global _CONFIG_PATH
    _CONFIG_PATH = path


def _load_config_file() -> dict:
    try:
        data = json.loads(_CONFIG_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _strip_transient_config(data: dict) -> dict:
    clean = {key: value for key, value in data.items() if key not in _TRANSIENT_CONFIG_KEYS}
    approval = clean.get("approval")
    if isinstance(approval, dict):
        approval = dict(approval)
        approval.pop("disable_confirmations", None)
        clean["approval"] = approval
    return clean


def _merge_config(existing: dict, updates: dict) -> dict:
    merged = dict(existing)
    for key, value in updates.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_config(current, value)
        else:
            merged[key] = value
    return merged


def _validate_save_payload(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Config payload must be a JSON object")

    clean = _strip_transient_config(data)
    llm = clean.get("llm")
    if not isinstance(llm, dict) or not llm:
        raise ValueError("Refusing to save config without a non-empty llm section")

    return clean


def _write_config_file(data: dict) -> None:
    tmp_path = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(_CONFIG_PATH)


def _human_side_effect_message(action: str, details: dict) -> str:
    label = _ACTION_LABELS.get(action, "执行设置操作")
    detail = ""
    if "key" in details:
        detail = f"\n\n记忆名称：{details['key']}"
    elif "rowid" in details:
        detail = f"\n\n记忆编号：{details['rowid']}"

    return (
        f"PawMate 准备{label}。{detail}\n\n"
        "这个操作会修改本地数据。是否继续？"
    )


class ConfigBridge(QObject):
    """Bridge for reading/saving config.json from the Web SettingsDrawer."""

    configSaved = Signal(str)
    configError = Signal(str)
    memoryReadyChanged = Signal()

    def __init__(
        self,
        parent=None,
        memory_service: MemorySettingsService | None = None,
        side_effect_confirm: Callable[[str, dict], bool] | None = None,
    ):
        super().__init__(parent)
        self._memory_service = memory_service
        self._side_effect_confirm = side_effect_confirm or self._default_side_effect_confirm
        self._side_effect_confirm_enabled = parent is not None or side_effect_confirm is not None
        logger.info("[ConfigBridge] initialized")

    def _mem_service(self) -> MemorySettingsService | None:
        return self._memory_service or _memory_settings_service

    def _confirm_side_effect(self, action: str, details: dict) -> bool:
        if action in _CONFIRM_BYPASS_ACTIONS:
            return True
        if not self._side_effect_confirm_enabled:
            return True
        try:
            allowed = bool(self._side_effect_confirm(action, details))
        except Exception as exc:
            logger.warning("[ConfigBridge] side-effect confirmation failed: %s", exc)
            return False
        if not allowed:
            logger.warning("[ConfigBridge] side-effect denied: %s", action)
        return allowed

    @staticmethod
    def _default_side_effect_confirm(action: str, details: dict) -> bool:
        if QApplication.instance() is None:
            return False
        result = QMessageBox.question(
            None,
            "PawMate 安全确认",
            _human_side_effect_message(action, details),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    @staticmethod
    def _denied_json(action: str) -> str:
        return json.dumps({
            "error": f"Action denied: {action}",
            "code": "config_bridge_side_effect_denied",
        }, ensure_ascii=False)

    # ============================================================
    # Slots callable from JS
    # ============================================================

    @Slot(result=str)
    def getConfig(self) -> str:
        """Return the current config.json as a JSON string, enriched with memory status."""
        try:
            data = _strip_transient_config(_load_config_file())
            service = self._mem_service()
            ms = service.get_memory_status() if service is not None else None
            if ms:
                data["_memory_status"] = ms
            logger.info("[ConfigBridge] getConfig: loaded OK")
            return json.dumps(data, ensure_ascii=False)
        except Exception as e:
            logger.error("[ConfigBridge] getConfig failed: %s", e)
            return "{}"

    @Slot(str)
    def saveConfig(self, json_text: str) -> None:
        """Receive a JSON string from JS and write it to config.json."""
        try:
            new_data = _validate_save_payload(json.loads(json_text))
            if not self._confirm_side_effect("save_config", {}):
                self.configError.emit("Action denied: save_config")
                return
            existing = _strip_transient_config(_load_config_file())
            merged = _merge_config(existing, new_data)
            _write_config_file(merged)
            logger.info("[ConfigBridge] saveConfig: saved OK")
            self.configSaved.emit("Settings saved successfully")
        except json.JSONDecodeError as e:
            logger.error("[ConfigBridge] saveConfig: invalid JSON: %s", e)
            self.configError.emit(f"Invalid JSON: {e}")
        except ValueError as e:
            logger.error("[ConfigBridge] saveConfig rejected: %s", e)
            self.configError.emit(str(e))
        except Exception as e:
            logger.error("[ConfigBridge] saveConfig failed: %s", e)
            self.configError.emit(f"Save failed: {e}")

    @Slot(result=str)
    def getMemoryStatus(self) -> str:
        """Return memory status as JSON."""
        service = self._mem_service()
        ms = service.get_memory_status() if service is not None else None
        if ms is not None:
            return json.dumps(ms)
        return json.dumps(default_memory_status())

    @Slot(str, result=str)
    def getAppLogs(self, limit_str: str = "300") -> str:
        """Return recent application log lines and log-file metadata."""
        try:
            from pawmate.core.app_logs import read_recent_logs

            limit = int(limit_str or "300")
            return json.dumps(read_recent_logs(limit), ensure_ascii=False)
        except Exception as e:
            logger.error("[Logs] read failed: %s", e)
            return json.dumps({"error": str(e), "lines": [], "text": ""}, ensure_ascii=False)

    @Slot(result=str)
    def clearAppLogs(self) -> str:
        """Clear the current application log file."""
        if not self._confirm_side_effect("clear_app_logs", {}):
            return self._denied_json("clear_app_logs")
        try:
            from pawmate.core.app_logs import clear_logs

            logger.info("[Logs] clearing app log by user request")
            return json.dumps(clear_logs(), ensure_ascii=False)
        except Exception as e:
            logger.error("[Logs] clear failed: %s", e)
            return json.dumps({"error": str(e), "lines": [], "text": ""}, ensure_ascii=False)

    # -- Core Memory CRUD slots --------------------------------------------

    @Slot(result=str)
    def getCoreMemories(self) -> str:
        """Return active core memories with ready/status state."""
        service = self._mem_service()
        if service is None:
            return json.dumps(initializing_items())
        try:
            return json.dumps(service.get_core_memories(), ensure_ascii=False)
        except Exception as e:
            logger.error("[Memory] getCoreMemories failed: %s", e)
            return json.dumps({"ready": False, "status": "error", "items": [], "error": str(e)})

    @Slot(result=str)
    def getEpisodicMemories(self) -> str:
        """Return recent episodic notes with ready/status state."""
        service = self._mem_service()
        if service is None:
            return json.dumps(initializing_items())
        try:
            return json.dumps(service.get_episodic_memories(50), ensure_ascii=False)
        except Exception as e:
            logger.error("[Memory] getEpisodicMemories failed: %s", e)
            return json.dumps({"ready": False, "status": "error", "items": [], "error": str(e)})

    @Slot(str, str)  # rowid, content
    def updateEpisodicMemory(self, rowid_str: str, content: str) -> None:
        """Update an episodic note's content by rowid."""
        if not self._confirm_side_effect("update_episodic_memory", {"rowid": rowid_str}):
            return
        service = self._mem_service()
        if service is None:
            return
        try:
            service.update_episodic_memory(int(rowid_str), content)
            logger.info("[Memory] episodic update: %s", rowid_str)
        except Exception as e:
            logger.error("[Memory] episodic update failed: %s", e)

    @Slot(str)  # rowid
    def deleteEpisodicMemory(self, rowid_str: str) -> None:
        """Delete an episodic note by rowid."""
        if not self._confirm_side_effect("delete_episodic_memory", {"rowid": rowid_str}):
            return
        service = self._mem_service()
        if service is None:
            return
        try:
            service.delete_episodic_memory(int(rowid_str))
            logger.info("[Memory] episodic delete: %s", rowid_str)
        except Exception as e:
            logger.error("[Memory] episodic delete failed: %s", e)

    @Slot(str, result=str)
    def getEpisodicSourceMessages(self, rowid_str: str) -> str:
        """Return source conversation messages linked to an episodic note."""
        service = self._mem_service()
        if service is None:
            return json.dumps(
                {"ready": False, "messages": [], "error": "memory not ready"},
                ensure_ascii=False,
            )
        try:
            return json.dumps(
                service.get_episodic_source_messages(int(rowid_str)),
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error("[Memory] episodic source load failed: %s", e)
            return json.dumps({"ready": False, "messages": [], "error": str(e)}, ensure_ascii=False)

    @Slot(str, str, str)  # key, value, pinned_str
    def createCoreMemory(self, key: str, value: str, pinned_str: str = "false") -> None:
        """Create a core memory entry (manual source)."""
        if not self._confirm_side_effect("create_core_memory", {"key": key}):
            return
        service = self._mem_service()
        if service is None:
            return
        pinned = pinned_str.lower() == "true"
        try:
            service.create_core_memory(key, value, pinned=pinned)
            logger.info("[Memory] manual create: %s", key)
        except Exception as e:
            logger.error("[Memory] create failed: %s", e)

    @Slot(str, str, str)
    def updateCoreMemory(self, key: str, value: str, pinned_str: str = "false") -> None:
        """Update a core memory entry (manual source)."""
        if not self._confirm_side_effect("update_core_memory", {"key": key}):
            return
        service = self._mem_service()
        if service is None:
            return
        pinned = pinned_str.lower() == "true"
        try:
            service.update_core_memory(key, value, pinned=pinned)
            logger.info("[Memory] manual update: %s", key)
        except Exception as e:
            logger.error("[Memory] update failed: %s", e)

    @Slot(str)
    def deleteCoreMemory(self, key: str) -> None:
        """Soft-delete a core memory (suppressed)."""
        if not self._confirm_side_effect("delete_core_memory", {"key": key}):
            return
        service = self._mem_service()
        if service is None:
            return
        try:
            service.delete_core_memory(key)
            logger.info("[Memory] manual delete: %s", key)
        except Exception as e:
            logger.error("[Memory] delete failed: %s", e)

    @Slot(str)
    def togglePinCoreMemory(self, key: str) -> None:
        """Toggle pinned status on a core memory."""
        if not self._confirm_side_effect("toggle_pin_core_memory", {"key": key}):
            return
        service = self._mem_service()
        if service is None:
            return
        try:
            service.toggle_pin_core_memory(key)
        except Exception as e:
            logger.error("[Memory] toggle pin failed: %s", e)
