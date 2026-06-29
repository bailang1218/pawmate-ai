"""QWebChannel bridge for Web UI chat.

ClawHub methods are async: they run in background threads so the UI
never freezes during network requests. Results arrive via
clawHubAsyncResponse signal.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from pawmate.qt_compat import QApplication, QMessageBox, QObject, Signal, Slot
from pawmate.bridge.skill_bridge import (
    SkillBridgeError,
    SkillPort,
    create_default_skill_bridge,
)

WEBBRIDGE_MAX_WORKERS = 4
WEBBRIDGE_MAX_PENDING = 16

_SIDE_EFFECT_LABELS = {
    "install_clawhub_skill": "安装技能包",
    "import_skill_folder": "导入本地技能文件夹",
    "import_skill_zip": "导入本地技能压缩包",
    "set_skill_status": "修改技能启用状态",
    "delete_skill": "删除技能",
    "review_skill": "标记技能审核状态",
}


def _human_side_effect_message(action: str, details: dict) -> str:
    label = _SIDE_EFFECT_LABELS.get(action, "执行本地操作")
    detail = ""
    if details.get("slug"):
        detail = f"\n\n技能：{details['slug']}"
    elif details.get("folder_path"):
        detail = f"\n\n文件夹：{details['folder_path']}"
    elif details.get("zip_path"):
        detail = f"\n\n压缩包：{details['zip_path']}"
    return f"PawMate 准备{label}。{detail}\n\n这个操作会修改本地技能或配置。是否继续？"


def _skills_result(ok: bool, data=None, error=None, error_code=None, error_details=None) -> str:
    """Wrap skills API result as JSON string.

    For error responses, includes error.code, error.message, and error.details
    so the frontend can display diagnostic information.
    """
    if ok:
        return json.dumps({"ok": True, **data}, ensure_ascii=False)
    err_obj = {"message": error or "Unknown error"}
    if error_code:
        err_obj["code"] = error_code
    if error_details:
        err_obj["details"] = error_details
    return json.dumps({"ok": False, "error": err_obj}, ensure_ascii=False)


def _extract_error_info(e: Exception):
    """Extract error code and details dict from exception.

    Returns (code, details_or_None).
    """
    if isinstance(e, SkillBridgeError):
        return e.code, e.details
    return "clawhub_unknown", None


def _emit_async(bridge, callback_id: str, result_json: str) -> None:
    """Emit async result on the Qt main thread via signal."""
    bridge.clawHubAsyncResponse.emit(json.dumps({
        "callback_id": callback_id,
        **json.loads(result_json),
    }, ensure_ascii=False))


class WebBridge(QObject):
    """Bridge object exposed to JavaScript through QWebChannel.

    Since PawMate 2.1 refactor: sendMessage() only forwards to
    ChatService via userMessageSubmitted. Mock logic lives in
    ChatService, not here.
    """

    appendAssistantMessage = Signal(str)  # full message (legacy mock fallback)
    appendAssistantDelta = Signal(str)    # streaming chunk
    appendAssistantDeltaForTurn = Signal(int, str)
    finalizeAssistant = Signal()          # streaming complete
    finalizeAssistantForTurn = Signal(int)
    appendUserMessage = Signal(str)
    appendToolCard = Signal(str, str, str, str)
    appendToolCardForTurn = Signal(int, str, str, str, str)
    taskListChanged = Signal(str)
    heartbeatStatus = Signal(str)         # JSON system heartbeat snapshot
    heartbeatWarning = Signal(str)        # JSON heartbeat warning
    presenceNudge = Signal(str)           # JSON non-chat presence nudge
    maintenanceTick = Signal(str)         # JSON maintenance tick
    # 工具确认请求 (发往 JS)
    toolConfirmRequest = Signal(str, str)  # tool_name, input_json
    setStatus = Signal(str)
    setInputEnabled = Signal(bool)        # enable/disable text input
    cancelRequested = Signal()                # user clicked stop
    closeRequested = Signal()                # close window
    minimizeRequested = Signal()             # minimize window
    maximizeRestoreRequested = Signal()      # toggle maximize/restore
    userMessageSubmitted = Signal(str)
    # ── 工具确认: JS 调用 resolveToolConfirm -> 这个信号 -> app -> engine
    toolConfirmResolved = Signal(bool)       # True=允许, False=拒绝

    # ── 任务中断信号 ──
    turnCancelled = Signal(int)  # 参数：已删除的消息数量
    # ── 任务运行状态 ──
    taskRunningChanged = Signal(bool)  # True=运行中, False=空闲

    # ---- Async ClawHub result signal ----
    # Emitted from background threads; Qt marshals to main thread automatically.
    # JSON payload: {"callback_id": "...", "ok": bool, "items|error": ...}
    clawHubAsyncResponse = Signal(str)

    def __init__(
        self,
        parent=None,
        mock_mode: bool = False,
        skill_port: SkillPort | None = None,
        side_effect_confirm: Callable[[str, dict], bool] | None = None,
    ):
        super().__init__(parent)
        self._logger = logging.getLogger("pawmate")
        self._logger.info("[WebBridge] mock_mode=%s", mock_mode)
        self._skill_port = skill_port or create_default_skill_bridge()
        self._side_effect_confirm = side_effect_confirm or self._default_side_effect_confirm
        self._executor = ThreadPoolExecutor(
            max_workers=WEBBRIDGE_MAX_WORKERS,
            thread_name_prefix="webbridge",
        )
        self._pending_lock = threading.Lock()
        self._pending_callbacks: set[str] = set()
        self._backend_status = "starting..."
        self._stream_trace_counts: dict[tuple[str, int], int] = {}
        self._last_stream_seq_by_turn: dict[int, int] = {}
        # mock_mode kept for backward compat; ChatService owns mock logic now.

    def _confirm_side_effect(self, action: str, details: dict) -> bool:
        try:
            allowed = bool(self._side_effect_confirm(action, details))
        except Exception as exc:
            self._logger.warning("[WebBridge] side-effect confirmation failed: %s", exc)
            return False
        if not allowed:
            self._logger.warning("[WebBridge] side-effect denied: %s", action)
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
    def _side_effect_denied_result(action: str) -> str:
        return _skills_result(
            False,
            error=f"Action denied: {action}",
            error_code="webbridge_side_effect_denied",
        )

    def _reject_async_side_effect(self, callback_id: str, action: str) -> str:
        _emit_async(self, callback_id, self._side_effect_denied_result(action))
        return self._PENDING

    def shutdown(self) -> None:
        """Stop background bridge workers without blocking app shutdown."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit_async(self, callback_id: str, target, *args) -> str:
        with self._pending_lock:
            if callback_id in self._pending_callbacks:
                _emit_async(
                    self,
                    callback_id,
                    _skills_result(
                        False,
                        error="Duplicate callback id",
                        error_code="webbridge_duplicate_callback",
                    ),
                )
                return self._PENDING
            if len(self._pending_callbacks) >= WEBBRIDGE_MAX_PENDING:
                _emit_async(
                    self,
                    callback_id,
                    _skills_result(
                        False,
                        error="Too many pending WebBridge requests",
                        error_code="webbridge_busy",
                    ),
                )
                return self._PENDING
            self._pending_callbacks.add(callback_id)

        def runner() -> None:
            try:
                target(callback_id, *args)
            finally:
                with self._pending_lock:
                    self._pending_callbacks.discard(callback_id)

        self._executor.submit(runner)
        return self._PENDING

    @Slot(str)
    def sendMessage(self, text: str) -> None:
        """Forward user message from JavaScript to ChatService."""
        message = text.strip()
        if not message:
            return
        self.appendUserMessage.emit(message)
        self.userMessageSubmitted.emit(message)

    @Slot(str)
    def relayStatus(self, text: str) -> None:
        """Store and relay backend readiness/status to JavaScript."""
        status = (text or "starting...").strip() or "starting..."
        self._backend_status = status
        self.setStatus.emit(status)

    @Slot(result=str)
    def getBackendStatus(self) -> str:
        """Return the latest backend status for QWebChannel late subscribers."""
        return self._backend_status

    @Slot(str)
    def relayAssistantDelta(self, text: str) -> None:
        """Relay one assistant stream delta to JavaScript."""
        value = "" if text is None else str(text)
        self._trace_stream("legacy", 0, value)
        self.appendAssistantDelta.emit(value)

    @Slot(int, str)
    def relayAssistantDeltaForTurn(self, turn_id: int, text: str) -> None:
        value = "" if text is None else str(text)
        turn = int(turn_id or 0)
        self._trace_stream("turn", turn, value)
        self.appendAssistantDeltaForTurn.emit(turn, value)

    @Slot(int, int, str)
    def relayAssistantDeltaForTurnSeq(self, turn_id: int, seq: int, text: str) -> None:
        turn = int(turn_id or 0)
        sequence = int(seq or 0)
        last = self._last_stream_seq_by_turn.get(turn, 0)
        if sequence and sequence <= last:
            self._logger.info(
                "[StreamTrace] bridge.turn_seq dropped duplicate turn=%s seq=%s last=%s",
                turn,
                sequence,
                last,
            )
            return
        if sequence:
            self._last_stream_seq_by_turn[turn] = sequence
        value = "" if text is None else str(text)
        self._trace_stream("turn_seq", turn, value)
        self.appendAssistantDeltaForTurn.emit(turn, value)

    @Slot(int)
    def finalizeAssistantTurn(self, turn_id: int) -> None:
        self.finalizeAssistantForTurn.emit(int(turn_id or 0))

    @Slot(str)
    def frontendTrace(self, payload: str) -> None:
        """Receive low-volume stream diagnostics from the Web UI."""
        self._logger.info("[FrontendTrace] %s", str(payload or "")[:500])

    def _trace_stream(self, channel: str, turn_id: int, value: str) -> None:
        key = (channel, int(turn_id or 0))
        count = self._stream_trace_counts.get(key, 0) + 1
        self._stream_trace_counts[key] = count
        if count <= 80 or count % 100 == 0:
            sample = value.replace("\n", "\\n")[:80]
            self._logger.info(
                "[StreamTrace] bridge.%s turn=%s seq=%s len=%s text=%r",
                channel,
                int(turn_id or 0),
                count,
                len(value),
                sample,
            )

    @Slot(str)
    def relayTaskList(self, payload_json: str) -> None:
        self.taskListChanged.emit(payload_json or "[]")

    # ── 工具确认 ───────────────────────────────────────────

    @Slot(bool)
    def resolveToolConfirm(self, allowed: bool) -> None:
        """
        JS 调用此方法反馈用户确认结果。
        App 监听 toolConfirmResolved 信号并调用 engine.resolve_confirm()。
        """
        self.toolConfirmResolved.emit(allowed)

    # ============================================================
    # Window control slots (called from JS via QWebChannel)
    # ============================================================

    @Slot()
    def cancelCurrentTask(self) -> None:
        """Called from JS stop button."""
        print("[WebBridge] cancelCurrentTask", flush=True)
        self.cancelRequested.emit()

    @Slot()
    def closeWindow(self) -> None:
        """Called from JS close button."""
        self.closeRequested.emit()

    @Slot()
    def minimizeWindow(self) -> None:
        """Called from JS minimize button."""
        self.minimizeRequested.emit()

    @Slot()
    def maximizeRestoreWindow(self) -> None:
        """Called from JS maximize/restore button."""
        self.maximizeRestoreRequested.emit()

    # ============================================================
    # ClawHub Skills — ASYNC (non-blocking, background threads)
    # ============================================================
    #
    # Each slot accepts a callback_id (generated by JS) and returns
    # immediately. The actual network request runs in a daemon thread.
    # When ready, the result is emitted via clawHubAsyncResponse signal
    # with the matching callback_id so JS can resolve the right promise.
    #
    # The return value is a JSON string the JS Promise resolves to,
    # but we mark it with {"code":"pending"} so JS ignores it and
    # waits for the signal-based result instead.

    _PENDING = '{"ok":false,"error":{"code":"pending","message":"Loading..."}}'

    @Slot(str, result=str)
    def listClawHubSkills(self, callback_id: str) -> str:
        """Async: list ClawHub skills (browse)."""
        return self._submit_async(callback_id, self._list_skills_async)

    def _list_skills_async(self, callback_id: str) -> None:
        try:
            _emit_async(
                self,
                callback_id,
                _skills_result(True, self._skill_port.list_clawhub_skills()),
            )
        except Exception as e:
            self._logger.error("[WebBridge] listSkills error: %s", e, exc_info=True)
            code, details = _extract_error_info(e)
            _emit_async(self, callback_id, _skills_result(False, error=str(e), error_code=code, error_details=details))

    @Slot(str, str, result=str)
    def searchClawHubSkills(self, callback_id: str, query: str) -> str:
        """Async: search ClawHub skills."""
        return self._submit_async(callback_id, self._search_skills_async, query)

    def _search_skills_async(self, callback_id: str, query: str) -> None:
        try:
            _emit_async(
                self,
                callback_id,
                _skills_result(True, self._skill_port.search_clawhub_skills(query)),
            )
        except Exception as e:
            self._logger.error("[WebBridge] searchSkills error: %s", e, exc_info=True)
            code, details = _extract_error_info(e)
            _emit_async(self, callback_id, _skills_result(False, error=str(e), error_code=code, error_details=details))

    @Slot(str, str, result=str)
    def getClawHubSkillDetail(self, callback_id: str, slug: str) -> str:
        """Async: get skill detail and SKILL.md content."""
        return self._submit_async(callback_id, self._get_detail_async, slug)

    def _get_detail_async(self, callback_id: str, slug: str) -> None:
        try:
            _emit_async(
                self,
                callback_id,
                _skills_result(True, self._skill_port.get_clawhub_skill_detail(slug)),
            )
        except Exception as e:
            self._logger.error("[WebBridge] getDetail error: %s", e, exc_info=True)
            code, details = _extract_error_info(e)
            _emit_async(self, callback_id, _skills_result(False, error=str(e), error_code=code, error_details=details))

    @Slot(str, str, str, result=str)
    @Slot(str, str, result=str)
    def installClawHubSkill(self, callback_id: str, slug: str, version: str = "") -> str:
        """Async: install a ClawHub skill."""
        action = "install_clawhub_skill"
        if not self._confirm_side_effect(action, {"slug": slug, "version": version}):
            return self._reject_async_side_effect(callback_id, action)
        return self._submit_async(callback_id, self._install_skill_async, slug, version)

    def _install_skill_async(self, callback_id: str, slug: str, version: str) -> None:
        try:
            _emit_async(
                self,
                callback_id,
                _skills_result(True, self._skill_port.install_clawhub_skill(slug, version)),
            )
        except Exception as e:
            self._logger.error("[WebBridge] installSkill error: %s", e, exc_info=True)
            code, details = _extract_error_info(e)
            _emit_async(self, callback_id, _skills_result(False, error=str(e), error_code=code, error_details=details))

    @Slot(str, result=str)
    def diagnoseClawHubConnection(self, callback_id: str) -> str:
        """Async: run lightweight diagnostics against ClawHub endpoints."""
        return self._submit_async(callback_id, self._diagnose_async)

    def _diagnose_async(self, callback_id: str) -> None:
        try:
            diag = self._skill_port.diagnose_clawhub_connection()
            _emit_async(self, callback_id, json.dumps({"ok": True, **diag}, ensure_ascii=False))
        except SkillBridgeError as e:
            self._logger.error("[WebBridge] diagnose error: %s", e, exc_info=True)
            _emit_async(self, callback_id, json.dumps({
                "ok": False,
                "error": {"message": str(e), "code": e.code, "details": e.details},
            }, ensure_ascii=False))
        except Exception as e:
            self._logger.error("[WebBridge] diagnose error: %s", e, exc_info=True)
            _emit_async(self, callback_id, json.dumps({
                "ok": False,
                "error": {"message": str(e), "code": "clawhub_unknown", "details": {"exception": repr(e)[:300]}},
            }, ensure_ascii=False))

    # ============================================================
    # Local import from folder/ZIP
    # ============================================================

    @Slot(result=str)
    def chooseSkillFolder(self) -> str:
        """Open folder selection dialog. Returns path or cancelled."""
        from pawmate.qt_compat import QFileDialog, QDir
        path = QFileDialog.getExistingDirectory(
            None, "Select Skill Folder", QDir.homePath(),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return json.dumps({"ok": False, "cancelled": True})
        return json.dumps({"ok": True, "path": path})

    @Slot(result=str)
    def chooseSkillZip(self) -> str:
        """Open ZIP file selection dialog. Returns path or cancelled."""
        from pawmate.qt_compat import QFileDialog, QDir
        path, _ = QFileDialog.getOpenFileName(
            None, "Select Skill ZIP", QDir.homePath(),
            "ZIP Files (*.zip);;All Files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return json.dumps({"ok": False, "cancelled": True})
        return json.dumps({"ok": True, "path": path})

    @Slot(str, str, result=str)
    def importLocalSkillFolder(self, callback_id: str, folder_path: str) -> str:
        """Async: import skill from local folder."""
        action = "import_local_skill_folder"
        if not self._confirm_side_effect(action, {"folder_path": folder_path}):
            return self._reject_async_side_effect(callback_id, action)
        return self._submit_async(callback_id, self._import_folder_async, folder_path)

    def _import_folder_async(self, callback_id: str, folder_path: str) -> None:
        try:
            _emit_async(
                self,
                callback_id,
                _skills_result(True, self._skill_port.import_local_skill_folder(folder_path)),
            )
        except SkillBridgeError as e:
            self._logger.error("[WebBridge] importFolder error: %s", e, exc_info=True)
            _emit_async(self, callback_id, _skills_result(False, error=str(e), error_code=e.code, error_details=e.details))
        except Exception as e:
            self._logger.error("[WebBridge] importFolder error: %s", e, exc_info=True)
            _emit_async(self, callback_id, _skills_result(False, error=str(e)))

    @Slot(str, str, result=str)
    def importLocalSkillZip(self, callback_id: str, zip_path: str) -> str:
        """Async: import skill from local ZIP."""
        action = "import_local_skill_zip"
        if not self._confirm_side_effect(action, {"zip_path": zip_path}):
            return self._reject_async_side_effect(callback_id, action)
        return self._submit_async(callback_id, self._import_zip_async, zip_path)

    def _import_zip_async(self, callback_id: str, zip_path: str) -> None:
        try:
            _emit_async(
                self,
                callback_id,
                _skills_result(True, self._skill_port.import_local_skill_zip(zip_path)),
            )
        except SkillBridgeError as e:
            self._logger.error("[WebBridge] importZip error: %s", e, exc_info=True)
            _emit_async(self, callback_id, _skills_result(False, error=str(e), error_code=e.code, error_details=e.details))
        except Exception as e:
            self._logger.error("[WebBridge] importZip error: %s", e, exc_info=True)
            _emit_async(self, callback_id, _skills_result(False, error=str(e)))

    # ============================================================
    # Sync skills slots (fast, non-network)
    # ============================================================

    @Slot(result=str)
    def listLocalSkills(self) -> str:
        """List all locally installed skills."""
        try:
            return _skills_result(True, self._skill_port.list_local_skills())
        except Exception as e:
            self._logger.error("[WebBridge] listSkills error: %s", e)
            return _skills_result(False, error=str(e))

    @Slot(str, str, result=str)
    def setSkillStatus(self, slug: str, new_status: str) -> str:
        """Update the status of an installed skill."""
        action = "set_skill_status"
        if not self._confirm_side_effect(action, {"slug": slug, "new_status": new_status}):
            return self._side_effect_denied_result(action)
        try:
            return _skills_result(True, self._skill_port.set_skill_status(slug, new_status))
        except SkillBridgeError as e:
            self._logger.error("[WebBridge] setSkillStatus error: %s", e)
            return _skills_result(False, error=str(e), error_code=e.code, error_details=e.details)
        except Exception as e:
            self._logger.error("[WebBridge] setSkillStatus error: %s", e)
            return _skills_result(False, error=str(e))

    @Slot(str, result=str)
    def getLocalSkillDetail(self, slug: str) -> str:
        """Get a local skill's metadata and SKILL.md content."""
        try:
            return _skills_result(True, self._skill_port.get_local_skill_detail(slug))
        except SkillBridgeError as e:
            self._logger.error("[WebBridge] getLocalSkillDetail error: %s", e)
            return _skills_result(False, error=str(e), error_code=e.code, error_details=e.details)
        except Exception as e:
            self._logger.error("[WebBridge] getLocalSkillDetail error: %s", e)
            return _skills_result(False, error=str(e))

    @Slot(result=str)
    def listBuiltinSkills(self) -> str:
        """List built-in PawMate skill templates."""
        try:
            return _skills_result(True, self._skill_port.list_builtin_skills())
        except Exception as e:
            self._logger.error("[WebBridge] listBuiltinSkills error: %s", e)
            return _skills_result(False, error=str(e))

    @Slot(str, result=str)
    def installBuiltinSkill(self, slug: str) -> str:
        """Install a built-in skill template to data/skills."""
        action = "install_builtin_skill"
        if not self._confirm_side_effect(action, {"slug": slug}):
            return self._side_effect_denied_result(action)
        try:
            return _skills_result(True, self._skill_port.install_builtin_skill(slug))
        except SkillBridgeError as e:
            self._logger.error("[WebBridge] installBuiltinSkill error: %s", e)
            return _skills_result(False, error=str(e), error_code=e.code, error_details=e.details)
        except Exception as e:
            self._logger.error("[WebBridge] installBuiltinSkill error: %s", e)
            return _skills_result(False, error=str(e))

    @Slot(str, result=str)
    def uninstallLocalSkill(self, slug: str) -> str:
        """Uninstall a local skill (backed up to _backup)."""
        action = "uninstall_local_skill"
        if not self._confirm_side_effect(action, {"slug": slug}):
            return self._side_effect_denied_result(action)
        try:
            return _skills_result(True, self._skill_port.uninstall_local_skill(slug))
        except SkillBridgeError as e:
            self._logger.error("[WebBridge] uninstallLocalSkill error: %s", e)
            return _skills_result(False, error=str(e), error_code=e.code, error_details=e.details)
        except Exception as e:
            self._logger.error("[WebBridge] uninstallLocalSkill error: %s", e)
            return _skills_result(False, error=str(e))
