"""
SQLite-backed session store for PawMate.

Per-thread connections via threading.local() — safe for PyQt6 + asyncio worker.
"""
import sqlite3
import json
import time
import threading
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from pawmate.core.safety.redaction import redact_sensitive_data
from pawmate.storage.app_paths import get_app_paths

logger = logging.getLogger("pawmate.session")

SCHEMA_VERSION = 1


class SessionStore:
    """Persists conversation sessions + messages in SQLite."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_app_paths().ensure_database_path()
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    # ── connection management ──────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ── schema ─────────────────────────────────────────────────

    def _init_schema(self):
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                summary TEXT,
                metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
            CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
        """)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # ── session CRUD ───────────────────────────────────────────

    def ensure_session(self, session_id: str, title: str = "对话") -> None:
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO sessions(id, title, created_at, updated_at)
                   VALUES(?, ?, ?, ?) ON CONFLICT(id) DO NOTHING""",
                (session_id, title, now, now),
            )

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT id, title, created_at, updated_at, message_count, summary
               FROM sessions ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def session_exists(self, session_id: str) -> bool:
        row = self._connect().execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        return row is not None

    def prune_empty_sessions(self, exclude_session_id: str = "") -> int:
        """Remove sessions that never received a message."""
        with self._transaction() as conn:
            if exclude_session_id:
                cur = conn.execute(
                    "DELETE FROM sessions WHERE message_count = 0 AND id != ?",
                    (exclude_session_id,),
                )
            else:
                cur = conn.execute("DELETE FROM sessions WHERE message_count = 0")
            return cur.rowcount

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._connect().execute(
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return default
        return row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, value),
            )

    def delete_session(self, session_id: str) -> bool:
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    def update_summary(self, session_id: str, summary: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE sessions SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, time.time(), session_id),
            )

    # ── messages ───────────────────────────────────────────────

    def append_message(self, session_id: str, role: str, content: Any) -> int:
        """Append one message. Returns its seq number."""
        content_str = json.dumps(redact_sensitive_data(content), ensure_ascii=False)
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT message_count FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO sessions(id, title, created_at, updated_at)
                       VALUES(?, ?, ?, ?)""",
                    (session_id, "对话", now, now),
                )
                seq = 0
            else:
                seq = row["message_count"]
            conn.execute(
                """INSERT INTO messages(session_id, seq, role, content, created_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (session_id, seq, role, content_str, now),
            )
            conn.execute(
                "UPDATE sessions SET message_count = ?, updated_at = ? WHERE id = ?",
                (seq + 1, now, session_id),
            )
        return seq

    def truncate_after_seq(self, session_id: str, seq: int) -> int:
        """
        删除指定 session 中所有 seq > given_seq 的消息。
        用于中断任务时回退到 snapshot。
        返回删除的消息数量。
        """
        with self._transaction() as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq > ?",
                (session_id, seq),
            )
            deleted = cur.rowcount
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            conn.execute(
                "UPDATE sessions SET message_count = ?, updated_at = ? WHERE id = ?",
                (row[0], time.time(), session_id),
            )
            return deleted

    def get_max_seq(self, session_id: str) -> int:
        """获取当前 session 的最大 seq（用于 snapshot）。"""
        row = self._connect().execute(
            "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 0

    def get_messages(self, session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        query = "SELECT seq, role, content, created_at FROM messages WHERE session_id = ? ORDER BY seq ASC"
        params: List[Any] = [session_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self._connect().execute(query, params).fetchall()
        result: List[Dict[str, Any]] = []
        for r in rows:
            try:
                content = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                content = r["content"]
            result.append({
                "seq": r["seq"],
                "role": r["role"],
                "content": redact_sensitive_data(content),
                "created_at": r["created_at"],
            })
        return result

    def get_messages_range(
        self,
        session_id: str,
        start_seq: int,
        end_seq: int,
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT seq, role, content, created_at FROM messages
               WHERE session_id = ? AND seq >= ? AND seq <= ?
               ORDER BY seq ASC LIMIT ?""",
            (session_id, int(start_seq), int(end_seq), int(limit)),
        ).fetchall()
        result: List[Dict[str, Any]] = []
        for r in rows:
            try:
                content = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                content = r["content"]
            result.append({
                "seq": r["seq"],
                "role": r["role"],
                "content": redact_sensitive_data(content),
                "created_at": r["created_at"],
            })
        return result

    def get_recent_messages(self, session_id: str, n: int) -> List[Dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT seq, role, content, created_at FROM messages WHERE session_id = ?
               ORDER BY seq DESC LIMIT ?""",
            (session_id, n),
        ).fetchall()
        rows = list(reversed(rows))
        result: List[Dict[str, Any]] = []
        for r in rows:
            try:
                content = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                content = r["content"]
            result.append({
                "seq": r["seq"],
                "role": r["role"],
                "content": redact_sensitive_data(content),
                "created_at": r["created_at"],
            })
        return result

    # ── maintenance ────────────────────────────────────────────

    def prune_old(self, keep_days: int = 90) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
            return cur.rowcount

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
