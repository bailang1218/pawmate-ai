"""
ConversationManager — 多对话管理器

职责：
  1. 创建 / 切换 / 删除 / 列出会话
  2. 与 AgentEngine 协调会话切换
  3. 通过 EventBus 通知 UI 会话变更

使用方式（集成到 main.py 或 web_bridge.py）：
    mgr = ConversationManager(engine, session_store)
    mgr.create_session("周报助手")
    mgr.switch_session("session_xxx")
    sessions = mgr.list_sessions()

暴露给 JS bridge 的方法：
    get_sessions() -> JSON string
    create_session(title) -> session_id
    switch_session(session_id) -> bool
    delete_session(session_id) -> bool
    rename_session(session_id, new_title) -> bool
    get_current_session_id() -> str
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from pawmate.app.ports.conversation_repository import ConversationRepository
from pawmate.qt_compat import QObject, Signal, Slot
from pawmate.storage.conversation_repository import SessionConversationRepository

if TYPE_CHECKING:
    from pawmate.core.engine import AgentEngine
    from pawmate.storage.session_store import SessionStore

_logger = logging.getLogger("pawmate")
_ACTIVE_SESSION_META_KEY = "active_session_id"


def _new_session_id() -> str:
    """生成短 session ID: 'sess_' + 8 位 hex"""
    return f"sess_{uuid.uuid4().hex[:8]}"


class ConversationManager(QObject):
    """
    多对话管理器。

    与 AgentEngine 的 HistoryStore 协同工作：
      - 每个 session 在 SQLite 中有独立的消息链
      - 切换 session 时更新 engine 的 HistoryStore
      - UI 通过 JS bridge 调用本类方法

    集成步骤：
      1. 在 web_bridge.py 中创建 ConversationManager 实例
      2. 用 registerObject 暴露给 QWebChannel
      3. 前端 JS 通过 bridge.conversationManager 调用方法
    """

    sessionChanged = Signal(str)

    def __init__(
        self,
        engine: Optional[AgentEngine] = None,
        session_store: Optional[SessionStore] = None,
        conversation_repository: Optional[ConversationRepository] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._engine = engine
        if conversation_repository is not None:
            self._repo = conversation_repository
            self._store = getattr(conversation_repository, "session_store", session_store)
        elif session_store is not None:
            self._repo = SessionConversationRepository(session_store=session_store)
            self._store = session_store
        else:
            self._repo = None
            self._store = None
        self._current_sid = ""
        self._currentSid = ""

    def _ensure_repo(self) -> ConversationRepository:
        if self._repo is None:
            repo = SessionConversationRepository()
            self._repo = repo
            self._store = repo.session_store
        return self._repo

    # ================================================================
    # JS Bridge 可调用方法（用 @Slot 标记）
    # ================================================================

    @Slot(result=str)
    def get_sessions(self) -> str:
        """
        获取所有会话列表。

        Returns:
            JSON 字符串，格式：
            [
              {
                "id": "sess_xxx",
                "title": "对话标题",
                "updated_at": 1716700000.0,
                "message_count": 12,
                "summary": "最近一条消息摘要...",
                "is_current": true
              },
              ...
            ]
        """
        try:
            if self._repo is None:
                return "[]"
            self._repo.prune_empty_sessions(exclude_session_id=self._current_sid)
            rows = self._repo.list_sessions(limit=100)
            result = []
            for row in rows:
                if int(row.get("message_count", 0) or 0) <= 0:
                    continue
                item = {
                    "id": row["id"],
                    "title": row.get("title", "对话"),
                    "updated_at": row.get("updated_at", 0),
                    "message_count": row.get("message_count", 0),
                    "summary": row.get("summary", ""),
                    "is_current": row["id"] == self._current_sid,
                }
                result.append(item)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            _logger.error("[ConversationManager] get_sessions failed: %s", e)
            return "[]"

    @Slot(str, result=str)
    def create_session(self, title: str = "") -> str:
        """
        创建新会话并切换到它。

        Args:
            title: 会话标题，留空则自动生成

        Returns:
            新会话的 session_id
        """
        try:
            sid = _new_session_id()
            if not title.strip():
                title = "新对话"
            repo = self._ensure_repo()
            repo.ensure_session(sid, title=title)
            _logger.info("[ConversationManager] created session %s: %s", sid, title)
            self._current_sid = sid
            self._currentSid = sid  # 兼容外部可能引用此属性的代码
            self._persist_active_session(sid)
            # 引擎就绪后才自动切换（占位对象 engine 为 None 时跳过）
            if self._engine is not None:
                self._switch_internal(sid)
            else:
                self.sessionChanged.emit(sid)
            return sid
        except Exception as e:
            _logger.error("[ConversationManager] create_session failed: %s", e)
            return ""

    @Slot(str, result=bool)
    def switch_session(self, session_id: str) -> bool:
        """
        切换到指定会话。

        Args:
            session_id: 目标会话 ID

        Returns:
            True=切换成功
        """
        try:
            if self._engine is not None and self._engine._state.is_running:
                _logger.warning("[ConversationManager] engine busy, cannot switch")
                return False

            if session_id == self._current_sid:
                return True

            self._switch_internal(session_id)
            return True
        except Exception as e:
            _logger.error("[ConversationManager] switch_session failed: %s", e)
            return False

    @Slot(str, result=bool)
    def delete_session(self, session_id: str) -> bool:
        """
        删除指定会话。

        如果删除的是当前会话，自动切换到最近的另一个会话；
        如果删除后没有剩余会话，自动创建一个新的。

        Args:
            session_id: 要删除的会话 ID

        Returns:
            True=删除成功
        """
        try:
            if self._engine is not None and self._engine._state.is_running:
                _logger.warning("[ConversationManager] engine busy, cannot delete")
                return False

            if self._repo is None:
                return False
            deleted = self._repo.delete_session(session_id)
            if not deleted:
                return False

            _logger.info("[ConversationManager] deleted session %s", session_id)

            # 如果删除的是当前会话，需要切换
            if session_id == self._current_sid:
                self._repo.prune_empty_sessions()
                remaining = [
                    row for row in self._repo.list_sessions(limit=100)
                    if int(row.get("message_count", 0) or 0) > 0
                ]
                if remaining:
                    self._switch_internal(remaining[0]["id"])
                else:
                    self._set_blank_session()

            return True
        except Exception as e:
            _logger.error("[ConversationManager] delete_session failed: %s", e)
            return False

    @Slot(str, str, result=bool)
    def rename_session(self, session_id: str, new_title: str) -> bool:
        """
        重命名会话。

        Args:
            session_id: 会话 ID
            new_title: 新标题

        Returns:
            True=重命名成功
        """
        try:
            new_title = new_title.strip()
            if not new_title:
                return False
            if self._repo is None:
                return False
            if not self._repo.rename_session(session_id, new_title):
                return False
            _logger.info("[ConversationManager] renamed %s -> %s", session_id, new_title)
            return True
        except Exception as e:
            _logger.error("[ConversationManager] rename_session failed: %s", e)
            return False

    @Slot(result=str)
    def get_current_session_id(self) -> str:
        """获取当前活跃的会话 ID"""
        return self._current_sid

    @Slot(result=bool)
    def start_blank_session(self) -> bool:
        """切换到空白草稿；下一条用户消息会创建新会话。"""
        try:
            if self._engine is not None and self._engine._state.is_running:
                _logger.warning("[ConversationManager] engine busy, cannot start blank session")
                return False
            self._set_blank_session()
            return True
        except Exception as e:
            _logger.error("[ConversationManager] start_blank_session failed: %s", e)
            return False

    @Slot(str, result=str)
    def get_session_messages(self, session_id: str) -> str:
        """
        获取指定会话的完整可见消息（用于 UI 恢复）。

        Returns:
            JSON 字符串，按 seq 升序排列的 user/assistant 消息
        """
        try:
            if self._repo is None:
                return "[]"
            messages = self._repo.get_messages(session_id)
            # 过滤掉工具消息，只返回 user/assistant
            filtered = []
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role in ("user", "assistant"):
                    # 如果 content 是 dict（带 tool_calls），提取文本部分
                    if isinstance(content, dict):
                        text = content.get("text", "")
                    elif isinstance(content, str):
                        text = content
                    else:
                        text = str(content)
                    if text.strip():
                        filtered.append({
                            "seq": msg.get("seq"),
                            "role": role,
                            "content": text,
                            "created_at": msg.get("created_at"),
                        })
            return json.dumps(filtered, ensure_ascii=False)
        except Exception as e:
            _logger.error("[ConversationManager] get_session_messages failed: %s", e)
            return "[]"

    @Slot(str, result=bool)
    def clear_session_messages(self, session_id: str) -> bool:
        """清空指定会话的所有消息（保留会话本身）。"""
        try:
            if self._engine is not None and self._engine._state.is_running:
                _logger.warning("[ConversationManager] engine busy, cannot clear messages")
                return False
            if self._repo is None:
                return False
            if not self._repo.clear_session_messages(session_id):
                return False
            _logger.info("[CM] cleared messages for session %s", session_id)
            # 如果是当前会话，需要重置 engine 的 history
            if session_id == self._current_sid and self._engine is not None:
                self._engine.replace_history_session(session_id)
            return True
        except Exception as e:
            _logger.error("[CM] clear_session_messages failed: %s", e)
            return False

    # ================================================================
    # 内部方法
    # ================================================================

    def _switch_internal(self, session_id: str) -> None:
        """内部切换会话逻辑。"""
        old_sid = self._current_sid
        if self._repo is not None:
            self._repo.ensure_session(session_id)
        if self._engine is not None:
            self._engine.replace_history_session(session_id)
        self._current_sid = session_id
        self._currentSid = session_id
        self._persist_active_session(session_id)
        msg_count = 0
        if self._engine is not None:
            msg_count = len(self._engine.get_history_messages())
        _logger.info(
            "[ConversationManager] switched %s -> %s (engine sees %d messages now)",
            old_sid, session_id, msg_count,
        )
        self.sessionChanged.emit(session_id)

    def _set_blank_session(self) -> None:
        old_sid = self._current_sid
        self._current_sid = ""
        self._currentSid = ""
        if self._repo is not None:
            try:
                self._repo.set_meta(_ACTIVE_SESSION_META_KEY, "")
            except Exception as e:
                _logger.warning("[ConversationManager] clear active session failed: %s", e)
        _logger.info("[ConversationManager] switched %s -> (blank)", old_sid)
        if self._engine is not None and hasattr(self._engine, "set_active_session"):
            self._engine.set_active_session("")
        self.sessionChanged.emit("")

    def set_engine(self, engine: "AgentEngine") -> None:
        """引擎就绪后调用，接入真实引擎。

        启动时不自动恢复上次会话。只有用户显式选择历史会话，
        或在空白页发送第一条消息时，才会设置当前会话。
        """
        self._engine = engine
        self._store = engine.get_history_session_store()
        self._repo = SessionConversationRepository(session_store=self._store)

        if self._current_sid:
            engine.replace_history_session(self._current_sid)
            self._currentSid = self._current_sid
            self._persist_active_session(self._current_sid)
            self.sessionChanged.emit(self._current_sid)

        _logger.info("[ConversationManager] engine wired: %s", self._current_sid or "(blank)")

    def _resolve_startup_session(self, fallback_sid: str) -> str:
        if self._repo is None:
            return fallback_sid

        saved_sid = self._repo.get_meta(_ACTIVE_SESSION_META_KEY, "")
        if saved_sid and self._repo.session_exists(saved_sid):
            return saved_sid

        sessions = self._repo.list_sessions(limit=1)
        if sessions:
            return sessions[0]["id"]

        self._repo.ensure_session(fallback_sid)
        return fallback_sid

    def _persist_active_session(self, session_id: str) -> None:
        if self._repo is None or not session_id:
            return
        try:
            self._repo.set_meta(_ACTIVE_SESSION_META_KEY, session_id)
        except Exception as e:
            _logger.warning("[ConversationManager] persist active session failed: %s", e)

    def auto_title_from_first_message(self, session_id: str) -> Optional[str]:
        """
        从第一条用户消息自动生成会话标题。
        在首条消息发送后调用。
        """
        try:
            if self._repo is None:
                return None
            messages = self._repo.get_messages(session_id, limit=1)
            if not messages:
                return None
            first = messages[0]
            content = first.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, dict):
                text = content.get("text", str(content))
            else:
                text = str(content)
            # 截取前 20 个字符作为标题
            title = text.strip()[:20]
            if len(text.strip()) > 20:
                title += "..."
            if title:
                self.rename_session(session_id, title)
                return title
        except Exception:
            pass
        return None
