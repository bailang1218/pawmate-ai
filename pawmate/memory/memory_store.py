"""
Shared SQLite connection for memory tables.

Reuses SessionStore's DB file (sessions.db), adds:
- core_memory + episodic_memory (existing)
- growth_state + memory_meta (v2)
"""
from __future__ import annotations

import sqlite3
import threading
import time
import json
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

from pawmate.memory.embedding import cosine_similarity

MEMORY_SCHEMA_VERSION = 3


class MemoryStore:
    """Shares the session DB file, adds memory-related tables."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path.home() / ".pawmate" / "sessions.db"
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()
        self._run_migrations()

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
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ── Schema ────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS core_memory (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory
            USING fts5(
                content,
                tags,
                metadata UNINDEXED,
                created_at UNINDEXED,
                tokenize = 'trigram'
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (component, version, updated_at) "
            "VALUES ('memory', 0, ?)",
            (time.time(),),
        )

    # ── Migration ─────────────────────────────────────────────

    def _run_migrations(self) -> None:
        current = self._get_schema_version()
        if current < 1:
            self._migrate_to_v1()
        if current < 2:
            self._migrate_to_v2()
        if current < 3:
            self._migrate_to_v3()

    def _get_schema_version(self) -> int:
        conn = self._connect()
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE component = 'memory'"
        ).fetchone()
        return row["version"] if row else 0

    def _set_schema_version(self, version: int) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE schema_migrations SET version = ?, updated_at = ? WHERE component = 'memory'",
            (version, time.time()),
        )

    def _migrate_to_v1(self) -> None:
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS growth_state (
                user_id TEXT NOT NULL,
                companion_id TEXT NOT NULL,
                affection INTEGER NOT NULL DEFAULT 0
                    CHECK (affection >= 0 AND affection <= 100),
                mood TEXT NOT NULL DEFAULT 'calm',
                stage TEXT NOT NULL DEFAULT 'stranger',
                streak INTEGER NOT NULL DEFAULT 0
                    CHECK (streak >= 0),
                conversation_count INTEGER NOT NULL DEFAULT 0
                    CHECK (conversation_count >= 0),
                last_seen REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, companion_id)
            );
            CREATE TABLE IF NOT EXISTS memory_meta (
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (scope_type, scope_id, key)
            );
        """)
        conn.execute(
            "INSERT OR IGNORE INTO growth_state "
            "(user_id, companion_id, affection, mood, stage, streak, "
            " conversation_count, last_seen, updated_at) "
            "VALUES (?, ?, 0, 'calm', 'stranger', 0, 0, 0, ?)",
            ("local-default", "default-pet", time.time()),
        )
        self._set_schema_version(1)

    def _migrate_to_v2(self) -> None:
        """Add source, status, pinned, deleted_at to core_memory (soft-delete support)."""
        conn = self._connect()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(core_memory)").fetchall()]
        now = time.time()
        if "source" not in cols:
            conn.execute("ALTER TABLE core_memory ADD COLUMN source TEXT NOT NULL DEFAULT 'ai'")
        if "status" not in cols:
            conn.execute("ALTER TABLE core_memory ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "pinned" not in cols:
            conn.execute("ALTER TABLE core_memory ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "deleted_at" not in cols:
            conn.execute("ALTER TABLE core_memory ADD COLUMN deleted_at REAL")
        self._set_schema_version(2)

    def _migrate_to_v3(self) -> None:
        """Add local vector database rows for hybrid memory retrieval."""
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_embedding (
                memory_type TEXT NOT NULL,
                memory_rowid INTEGER NOT NULL,
                embedding_model TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (memory_type, memory_rowid, embedding_model)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_embedding_type_model
                ON memory_embedding(memory_type, embedding_model);
        """)
        self._set_schema_version(3)

    def upsert_embedding(
        self,
        *,
        memory_type: str,
        memory_rowid: int,
        embedding_model: str,
        vector: list[float],
    ) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO memory_embedding "
            "(memory_type, memory_rowid, embedding_model, dim, vector_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                memory_type,
                int(memory_rowid),
                embedding_model,
                len(vector),
                json.dumps(vector, separators=(",", ":")),
                time.time(),
            ),
        )

    def delete_embedding(
        self,
        *,
        memory_type: str,
        memory_rowid: int,
        embedding_model: str | None = None,
    ) -> None:
        conn = self._connect()
        if embedding_model:
            conn.execute(
                "DELETE FROM memory_embedding WHERE memory_type = ? AND memory_rowid = ? AND embedding_model = ?",
                (memory_type, int(memory_rowid), embedding_model),
            )
        else:
            conn.execute(
                "DELETE FROM memory_embedding WHERE memory_type = ? AND memory_rowid = ?",
                (memory_type, int(memory_rowid)),
            )

    def search_embeddings(
        self,
        *,
        memory_type: str,
        embedding_model: str,
        query_vector: list[float],
        k: int = 5,
    ) -> list[tuple[int, float]]:
        rows = self._connect().execute(
            "SELECT memory_rowid, vector_json FROM memory_embedding "
            "WHERE memory_type = ? AND embedding_model = ?",
            (memory_type, embedding_model),
        ).fetchall()
        scored: list[tuple[int, float]] = []
        for row in rows:
            try:
                vector = json.loads(row["vector_json"])
            except Exception:
                continue
            score = cosine_similarity(query_vector, vector)
            if score > 0:
                scored.append((int(row["memory_rowid"]), float(score)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[: max(1, int(k))]

    # ── Meta KV ───────────────────────────────────────────────

    def get_meta(
        self,
        scope_type: str,
        scope_id: str,
        key: str,
        default: Optional[str] = None,
    ) -> Optional[str]:
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM memory_meta "
            "WHERE scope_type = ? AND scope_id = ? AND key = ?",
            (scope_type, scope_id, key),
        ).fetchone()
        return row["value"] if row else default

    def set_meta(
        self,
        scope_type: str,
        scope_id: str,
        key: str,
        value: str,
    ) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta "
            "(scope_type, scope_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope_type, scope_id, key, value, time.time()),
        )

    # ── Count helpers ─────────────────────────────────────────

    def count_core_memories(self) -> int:
        conn = self._connect()
        row = conn.execute("SELECT COUNT(*) as cnt FROM core_memory").fetchone()
        return row["cnt"] if row else 0

    def count_episodic_memories(self) -> int:
        conn = self._connect()
        row = conn.execute("SELECT COUNT(*) as cnt FROM episodic_memory").fetchone()
        return row["cnt"] if row else 0

    # ── Close ─────────────────────────────────────────────────

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
