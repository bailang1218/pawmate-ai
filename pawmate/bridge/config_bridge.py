"""QWebChannel bridge for reading/saving config.json from the Web UI."""
from __future__ import annotations

import json
import logging
import os
import threading
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
    "create_memory_item": "新增一条记忆",
    "update_memory_item": "修改一条记忆",
    "delete_memory_item": "把一条记忆移到回收站",
    "restore_memory_item": "从回收站恢复一条记忆",
    "update_episodic_memory": "修改一条长期记忆",
    "delete_episodic_memory": "删除一条长期记忆",
    "create_core_memory": "新增核心记忆",
    "update_core_memory": "修改核心记忆",
    "delete_core_memory": "删除核心记忆",
    "toggle_pin_core_memory": "切换核心记忆置顶状态",
}

_memory_settings_service: MemorySettingsService | None = None
_bridge_instance = None
_CONFIG_LOCK = threading.RLock()
_SECRET_KEYS = {"api_key", "apikey", "auth_token", "access_token", "refresh_token", "password", "secret"}


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
        from pawmate.storage.secret_codec import unprotect_config
        data = unprotect_config(json.loads(_CONFIG_PATH.read_text("utf-8")))
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
        if key.lower() in _SECRET_KEYS and (value is None or str(value).strip() == ""):
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_config(current, value)
        else:
            merged[key] = value
    return merged


def _public_config(value):
    """Return configuration safe to expose to JavaScript."""
    if isinstance(value, dict):
        return {
            key: ("" if key.lower() in _SECRET_KEYS else _public_config(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_config(item) for item in value]
    return value


def _validate_save_payload(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Config payload must be a JSON object")

    clean = _strip_transient_config(data)
    llm = clean.get("llm")
    if not isinstance(llm, dict) or not llm:
        raise ValueError("Refusing to save config without a non-empty llm section")

    for provider, provider_config in llm.items():
        if not isinstance(provider_config, dict):
            continue
        api_key = str(provider_config.get("api_key") or "").strip()
        if not api_key:
            continue
        if len(api_key) > 4096:
            raise ValueError(
                f"{str(provider).upper()} API 密钥内容过长，请勿粘贴日志或整段文字。"
            )
        if not api_key.isascii() or any(char.isspace() for char in api_key):
            raise ValueError(
                f"{str(provider).upper()} API 密钥只能包含 ASCII 字符且不能包含空格。"
            )

    return clean


def _write_config_file(data: dict) -> None:
    from pawmate.storage.secret_codec import protect_config
    with _CONFIG_LOCK:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(protect_config(data), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp_path, _CONFIG_PATH)


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
            data = _public_config(_strip_transient_config(_load_config_file()))
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
            with _CONFIG_LOCK:
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
            from pawmate.core.observability.app_logs import read_recent_logs

            limit = int(limit_str or "300")
            return json.dumps(read_recent_logs(limit), ensure_ascii=False)
        except Exception as e:
            logger.error("[Logs] read failed: %s", e)
            return json.dumps({"error": str(e), "lines": [], "text": ""}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def getAppLogsByKind(self, kind: str = "app", limit_str: str = "300") -> str:
        """Return recent log lines for one logical log stream."""
        try:
            from pawmate.core.observability.app_logs import read_recent_logs

            limit = int(limit_str or "300")
            return json.dumps(read_recent_logs(limit, kind=kind), ensure_ascii=False)
        except Exception as e:
            logger.error("[Logs] read %s failed: %s", kind, e)
            return json.dumps({"error": str(e), "lines": [], "text": ""}, ensure_ascii=False)

    @Slot(result=str)
    def clearAppLogs(self) -> str:
        """Clear the current application log file."""
        if not self._confirm_side_effect("clear_app_logs", {}):
            return self._denied_json("clear_app_logs")
        try:
            from pawmate.core.observability.app_logs import clear_logs

            logger.info("[Logs] clearing app log by user request")
            return json.dumps(clear_logs(), ensure_ascii=False)
        except Exception as e:
            logger.error("[Logs] clear failed: %s", e)
            return json.dumps({"error": str(e), "lines": [], "text": ""}, ensure_ascii=False)

    @Slot(str, result=str)
    def clearAppLogsByKind(self, kind: str = "app") -> str:
        """Clear one logical application log stream."""
        if not self._confirm_side_effect("clear_app_logs", {"kind": kind}):
            return self._denied_json("clear_app_logs")
        try:
            from pawmate.core.observability.app_logs import clear_logs

            logger.info("[Logs] clearing %s log by user request", kind)
            return json.dumps(clear_logs(kind=kind), ensure_ascii=False)
        except Exception as e:
            logger.error("[Logs] clear %s failed: %s", kind, e)
            return json.dumps({"error": str(e), "lines": [], "text": ""}, ensure_ascii=False)

    @staticmethod
    def _workspace_path(raw_path: str = "") -> Path:
        root = Path.cwd().resolve()
        target = (root / str(raw_path or ".")).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Path must stay inside the PawMate workspace") from exc
        return target

    @Slot(str, result=str)
    def listWorkspaceFiles(self, raw_path: str = "") -> str:
        """List one workspace directory without exposing paths outside the project."""
        try:
            root = Path.cwd().resolve()
            directory = self._workspace_path(raw_path)
            if not directory.is_dir():
                raise ValueError("Directory does not exist")
            entries = []
            for item in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if item.name in {".git", ".venv", ".venv311", "node_modules", "__pycache__"}:
                    continue
                entries.append({
                    "name": item.name,
                    "path": item.relative_to(root).as_posix(),
                    "directory": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else 0,
                })
                if len(entries) >= 500:
                    break
            relative = directory.relative_to(root).as_posix()
            return json.dumps({"ok": True, "path": relative, "entries": entries}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc), "entries": []}, ensure_ascii=False)

    @Slot(str, result=str)
    def readWorkspaceFile(self, raw_path: str) -> str:
        try:
            path = self._workspace_path(raw_path)
            from pawmate.core.safety.file_boundary import is_secret_path
            if is_secret_path(path):
                raise ValueError("Secret-like files cannot be opened in the workspace editor")
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("File does not exist or exceeds 2 MB")
            return json.dumps({"ok": True, "path": raw_path, "content": path.read_text("utf-8")}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def saveWorkspaceFile(self, raw_path: str, content: str) -> str:
        try:
            path = self._workspace_path(raw_path)
            if not path.is_file():
                raise ValueError("Only existing workspace files can be saved")
            if len(content.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("File exceeds 2 MB")
            if not self._confirm_side_effect("save_workspace_file", {"path": raw_path}):
                return self._denied_json("save_workspace_file")
            from pawmate.core.safety.file_boundary import is_secret_path
            if is_secret_path(path):
                raise ValueError("Secret-like files cannot be edited in the workspace editor")
            tmp_path = path.with_suffix(path.suffix + ".pawmate.tmp")
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, path)
            return json.dumps({"ok": True, "path": raw_path}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # -- Layered Memory v3 slots -------------------------------------------

    @Slot(str, result=str)
    def queryMemoryItems(self, request_json: str = "{}") -> str:
        service = self._mem_service()
        if service is None:
            return json.dumps(initializing_items())
        try:
            request = json.loads(request_json or "{}")
            if not isinstance(request, dict):
                raise ValueError("memory query must be a JSON object")
            return json.dumps(service.query_memory_items(request), ensure_ascii=False)
        except Exception as exc:
            logger.error("[Memory] item query failed: %s", exc)
            return json.dumps({"ready": False, "status": "error", "items": [], "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def createMemoryItem(self, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json or "{}")
            if not isinstance(payload, dict):
                raise ValueError("memory payload must be a JSON object")
            if not self._confirm_side_effect(
                "create_memory_item",
                {"item_type": payload.get("item_type"), "predicate": payload.get("predicate")},
            ):
                return self._denied_json("create_memory_item")
            service = self._mem_service()
            if service is None:
                raise RuntimeError("memory not ready")
            return json.dumps({"ok": True, "item": service.create_memory_item(payload)}, ensure_ascii=False)
        except Exception as exc:
            logger.error("[Memory] item create failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def updateMemoryItem(self, item_id: str, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json or "{}")
            if not isinstance(payload, dict):
                raise ValueError("memory payload must be a JSON object")
            if not self._confirm_side_effect("update_memory_item", {"item_id": item_id}):
                return self._denied_json("update_memory_item")
            service = self._mem_service()
            if service is None:
                raise RuntimeError("memory not ready")
            return json.dumps(
                {"ok": True, "item": service.update_memory_item(item_id, payload)},
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.error("[Memory] item update failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def deleteMemoryItem(self, item_id: str) -> str:
        if not self._confirm_side_effect("delete_memory_item", {"item_id": item_id}):
            return self._denied_json("delete_memory_item")
        service = self._mem_service()
        try:
            if service is None:
                raise RuntimeError("memory not ready")
            return json.dumps({"ok": bool(service.delete_memory_item(item_id))}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def restoreMemoryItem(self, item_id: str) -> str:
        if not self._confirm_side_effect("restore_memory_item", {"item_id": item_id}):
            return self._denied_json("restore_memory_item")
        service = self._mem_service()
        try:
            if service is None:
                raise RuntimeError("memory not ready")
            return json.dumps({"ok": bool(service.restore_memory_item(item_id))}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def queryConversationArchive(self, request_json: str = "{}") -> str:
        service = self._mem_service()
        if service is None:
            return json.dumps(initializing_items())
        try:
            request = json.loads(request_json or "{}")
            return json.dumps(service.query_conversation_archive(request), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ready": False, "status": "error", "items": [], "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def searchConversationArchive(self, request_json: str = "{}") -> str:
        service = self._mem_service()
        if service is None:
            return json.dumps(initializing_items())
        try:
            request = json.loads(request_json or "{}")
            return json.dumps(service.search_conversation_archive(request), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ready": False, "status": "error", "items": [], "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def openHistoryContext(self, request_json: str) -> str:
        service = self._mem_service()
        if service is None:
            return json.dumps({"ready": False, "messages": [], "error": "memory not ready"})
        try:
            request = json.loads(request_json or "{}")
            return json.dumps(service.open_history_context(request), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ready": False, "messages": [], "error": str(exc)}, ensure_ascii=False)

    # -- Legacy Memory CRUD slots -----------------------------------------

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
            return json.dumps(service.get_episodic_memories(), ensure_ascii=False)
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
