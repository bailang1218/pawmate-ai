"""SQLite-backed implementation of the conversation repository port."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pawmate.storage.session_store import SessionStore


class SessionConversationRepository:
    """ConversationRepository backed by SessionStore."""

    def __init__(
        self,
        session_store: SessionStore | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self._store = session_store or SessionStore(db_path=Path(db_path) if db_path else None)

    @property
    def session_store(self) -> SessionStore:
        return self._store

    def ensure_session(self, session_id: str, title: str = "对话") -> None:
        self._store.ensure_session(session_id, title=title)

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.list_sessions(limit=limit)

    def delete_session(self, session_id: str) -> bool:
        return self._store.delete_session(session_id)

    def session_exists(self, session_id: str) -> bool:
        return self._store.session_exists(session_id)

    def prune_empty_sessions(self, exclude_session_id: str = "") -> int:
        return self._store.prune_empty_sessions(exclude_session_id=exclude_session_id)

    def get_meta(self, key: str, default: str = "") -> str:
        value = self._store.get_meta(key, default)
        return default if value is None else value

    def set_meta(self, key: str, value: str) -> None:
        self._store.set_meta(key, value)

    def get_messages(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.get_messages(session_id, limit=limit)

    def get_messages_range(
        self,
        session_id: str,
        start_seq: int,
        end_seq: int,
    ) -> list[dict[str, Any]]:
        return self._store.get_messages_range(session_id, start_seq, end_seq)

    def rename_session(self, session_id: str, new_title: str) -> bool:
        conn = self._store._connect()
        cur = conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (new_title, time.time(), session_id),
        )
        return cur.rowcount > 0

    def clear_session_messages(self, session_id: str) -> bool:
        with self._store._transaction() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,),
            )
            cur = conn.execute(
                "UPDATE sessions SET message_count = 0, updated_at = ? WHERE id = ?",
                (time.time(), session_id),
            )
        return cur.rowcount > 0

    def close(self) -> None:
        self._store.close()


__all__ = ["SessionConversationRepository"]
