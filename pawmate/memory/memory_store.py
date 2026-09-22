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
import hashlib
import uuid
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

from pawmate.memory.embedding import cosine_similarity
from pawmate.storage.app_paths import get_app_paths

MEMORY_SCHEMA_VERSION = 7


class MemoryStore:
    """Shares the session DB file, adds memory-related tables."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_app_paths().ensure_database_path()
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migration_backup_path = self._backup_before_upgrade()
        self._init_schema()
        self._run_migrations()

    def _backup_before_upgrade(self) -> Path | None:
        """Take a consistent SQLite backup before the first v6 migration."""
        if not self.db_path.is_file() or self.db_path.stat().st_size <= 0:
            return None
        source = None
        try:
            source = sqlite3.connect(str(self.db_path), isolation_level=None)
            table = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if table:
                row = source.execute(
                    "SELECT version FROM schema_migrations WHERE component = 'memory'"
                ).fetchone()
                current = int(row[0]) if row else 0
            else:
                current = 0
            if current >= MEMORY_SCHEMA_VERSION:
                return None
            backup_dir = self.db_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            backup_path = backup_dir / (
                f"{self.db_path.stem}-before-memory-v{MEMORY_SCHEMA_VERSION}-{stamp}.db"
            )
            target = sqlite3.connect(str(backup_path))
            try:
                source.backup(target)
            finally:
                target.close()
            return backup_path
        except sqlite3.DatabaseError:
            return None
        finally:
            if source is not None:
                source.close()

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
        if current < 4:
            self._migrate_to_v4()
        if current < 5:
            self._migrate_to_v5()
        if current < 6:
            self._migrate_to_v6()
        if current < 7:
            self._migrate_to_v7()

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

    def _migrate_to_v4(self) -> None:
        """Create the unified, auditable long-term memory store."""
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_items (
                id TEXT PRIMARY KEY,
                item_type TEXT NOT NULL
                    CHECK (item_type IN ('profile', 'fact', 'event', 'lesson')),
                scope_type TEXT NOT NULL DEFAULT 'global',
                scope_id TEXT NOT NULL DEFAULT 'local-default',
                subject TEXT NOT NULL DEFAULT '',
                predicate TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'pending_review', 'superseded', 'deleted')),
                pinned INTEGER NOT NULL DEFAULT 0,
                source_kind TEXT NOT NULL DEFAULT 'manual',
                source_session_id TEXT NOT NULL DEFAULT '',
                source_start_seq INTEGER,
                source_end_seq INTEGER,
                valid_from REAL,
                valid_until REAL,
                content_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_accessed_at REAL,
                access_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_memory_items_type_status
                ON memory_items(item_type, status, pinned DESC, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_items_scope_status
                ON memory_items(scope_type, scope_id, status);
            CREATE INDEX IF NOT EXISTS idx_memory_items_source
                ON memory_items(source_session_id, source_start_seq, source_end_seq);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_active_hash
                ON memory_items(scope_type, scope_id, item_type, content_hash)
                WHERE status IN ('active', 'pending_review');

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_item_fts
            USING fts5(
                item_id UNINDEXED,
                item_type UNINDEXED,
                scope_id UNINDEXED,
                subject,
                predicate,
                content,
                tokenize = 'trigram'
            );

            CREATE TABLE IF NOT EXISTS memory_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_audit_item
                ON memory_audit_log(item_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS semantic_embeddings (
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (object_type, object_id, embedding_model)
            );
            CREATE INDEX IF NOT EXISTS idx_semantic_embeddings_type_model
                ON semantic_embeddings(object_type, embedding_model);
        """)
        self._set_schema_version(4)

    def _migrate_to_v5(self) -> None:
        """Create rebuildable session summaries and a full raw-message archive index."""
        conn = self._connect()
        # MemoryStore and SessionStore share one SQLite file. Creating the source
        # tables here is safe and makes migrations deterministic in fresh tests.
        conn.executescript("""
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

            CREATE TABLE IF NOT EXISTS session_summaries (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                topics_json TEXT NOT NULL DEFAULT '[]',
                covered_start_seq INTEGER,
                covered_end_seq INTEGER,
                summary_version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_session_summaries_updated
                ON session_summaries(updated_at DESC);

            CREATE TABLE IF NOT EXISTS message_chunks (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                start_seq INTEGER NOT NULL,
                end_seq INTEGER NOT NULL,
                chunk_kind TEXT NOT NULL DEFAULT 'turn',
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(session_id, start_seq, end_seq, chunk_kind),
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_message_chunks_session
                ON message_chunks(session_id, start_seq, end_seq);

            CREATE VIRTUAL TABLE IF NOT EXISTS session_summary_fts
            USING fts5(
                session_id UNINDEXED,
                title,
                summary,
                topics,
                tokenize = 'trigram'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS message_chunk_fts
            USING fts5(
                chunk_id UNINDEXED,
                session_id UNINDEXED,
                title,
                summary,
                content,
                tokenize = 'trigram'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS message_archive_fts
            USING fts5(
                message_id UNINDEXED,
                session_id UNINDEXED,
                seq UNINDEXED,
                role UNINDEXED,
                content,
                created_at UNINDEXED,
                tokenize = 'trigram'
            );

            CREATE TRIGGER IF NOT EXISTS messages_memory_fts_ai
            AFTER INSERT ON messages BEGIN
                INSERT OR REPLACE INTO message_archive_fts(
                    rowid, message_id, session_id, seq, role, content, created_at
                ) VALUES (new.id, new.id, new.session_id, new.seq, new.role, new.content, new.created_at);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_memory_fts_ad
            AFTER DELETE ON messages BEGIN
                DELETE FROM message_archive_fts WHERE rowid = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS messages_memory_fts_au
            AFTER UPDATE ON messages BEGIN
                DELETE FROM message_archive_fts WHERE rowid = old.id;
                INSERT OR REPLACE INTO message_archive_fts(
                    rowid, message_id, session_id, seq, role, content, created_at
                ) VALUES (new.id, new.id, new.session_id, new.seq, new.role, new.content, new.created_at);
            END;
        """)
        # The raw message index is derived data. Rebuilding it is safe and also
        # fills the index for conversations created before this migration.
        conn.execute("DELETE FROM message_archive_fts")
        conn.execute(
            "INSERT INTO message_archive_fts(rowid, message_id, session_id, seq, role, content, created_at) "
            "SELECT id, id, session_id, seq, role, content, created_at FROM messages"
        )
        self._set_schema_version(5)

    def _migrate_to_v6(self) -> None:
        """Import legacy core rows without trusting old AI-generated content."""
        conn = self._connect()
        marker = conn.execute(
            "SELECT value FROM memory_meta WHERE scope_type = 'system' "
            "AND scope_id = 'memory' AND key = 'legacy_core_import_v6'"
        ).fetchone()
        if marker is None:
            rows = conn.execute(
                "SELECT key, value, source, status, pinned, created_at, updated_at "
                "FROM core_memory"
            ).fetchall()
            for row in rows:
                key = str(row["key"] or "").strip()
                value = str(row["value"] or "").strip()
                if not key or not value:
                    continue
                source = str(row["source"] or "ai")
                legacy_status = str(row["status"] or "active")
                if legacy_status != "active":
                    status = "deleted"
                elif source == "manual":
                    status = "active"
                else:
                    # Old AI writes were created under a permissive policy.
                    # Keep them for review but never inject them automatically.
                    status = "pending_review"
                item_type = self._legacy_item_type(key)
                canonical = "\n".join(["user", key.lower(), value])
                content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pawmate:legacy-core:{key}"))
                try:
                    conn.execute(
                        "INSERT INTO memory_items ("
                        "id, item_type, scope_type, scope_id, subject, predicate, content, "
                        "importance, confidence, status, pinned, source_kind, content_hash, "
                        "metadata_json, created_at, updated_at) "
                        "VALUES (?, ?, 'global', 'local-default', 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item_id,
                            item_type,
                            key,
                            value,
                            0.8 if bool(row["pinned"]) else 0.55,
                            1.0 if source == "manual" else 0.5,
                            status,
                            int(bool(row["pinned"])),
                            f"legacy_{source}",
                            content_hash,
                            json.dumps({"legacy_key": key}, ensure_ascii=False),
                            float(row["created_at"] or time.time()),
                            float(row["updated_at"] or time.time()),
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                if status != "deleted":
                    conn.execute(
                        "INSERT INTO memory_item_fts(item_id, item_type, scope_id, subject, predicate, content) "
                        "VALUES (?, ?, 'local-default', 'user', ?, ?)",
                        (item_id, item_type, key, value),
                    )
                conn.execute(
                    "INSERT INTO memory_audit_log(item_id, action, actor, reason, payload_json, created_at) "
                    "VALUES (?, 'import', 'migration', 'legacy_core_v6', '{}', ?)",
                    (item_id, time.time()),
                )
            conn.execute(
                "INSERT OR REPLACE INTO memory_meta(scope_type, scope_id, key, value, updated_at) "
                "VALUES ('system', 'memory', 'legacy_core_import_v6', 'complete', ?)",
                (time.time(),),
            )
        self._set_schema_version(6)

    def _migrate_to_v7(self) -> None:
        """Backfill rebuildable summaries and bounded legacy message windows."""
        conn = self._connect()
        conn.executescript("""
            INSERT OR IGNORE INTO session_summaries (
                session_id, title, summary, topics_json, covered_start_seq,
                covered_end_seq, summary_version, created_at, updated_at
            )
            SELECT
                s.id,
                COALESCE(NULLIF(TRIM(s.title), ''), '对话'),
                COALESCE(
                    NULLIF(TRIM(s.summary), ''),
                    (
                        SELECT SUBSTR(GROUP_CONCAT(sample.role || ': ' || sample.content, CHAR(10)), 1, 2500)
                        FROM (
                            SELECT role, content
                            FROM messages
                            WHERE session_id = s.id
                            ORDER BY seq ASC
                            LIMIT 12
                        ) AS sample
                    ),
                    ''
                ),
                '[]',
                (SELECT MIN(seq) FROM messages WHERE session_id = s.id),
                (SELECT MAX(seq) FROM messages WHERE session_id = s.id),
                1,
                s.created_at,
                s.updated_at
            FROM sessions AS s;

            DELETE FROM session_summary_fts;
            INSERT INTO session_summary_fts(session_id, title, summary, topics)
            SELECT session_id, title, summary, '' FROM session_summaries;

            INSERT OR IGNORE INTO message_chunks (
                id, session_id, start_seq, end_seq, chunk_kind, title,
                summary, content, created_at, updated_at
            )
            SELECT
                LOWER(HEX(RANDOMBLOB(16))),
                grouped.session_id,
                MIN(grouped.seq),
                MAX(grouped.seq),
                'legacy_window',
                COALESCE(NULLIF(TRIM(MAX(grouped.session_title)), ''), '历史对话'),
                SUBSTR(GROUP_CONCAT(grouped.role || ': ' || grouped.content, CHAR(10)), 1, 1200),
                SUBSTR(GROUP_CONCAT('[' || grouped.seq || '] ' || grouped.role || ': ' || grouped.content, CHAR(10)), 1, 8000),
                MIN(grouped.created_at),
                MAX(grouped.created_at)
            FROM (
                SELECT
                    m.session_id, m.seq, m.role, m.content, m.created_at,
                    CAST(m.seq / 8 AS INTEGER) AS window_no,
                    s.title AS session_title
                FROM messages AS m
                JOIN sessions AS s ON s.id = m.session_id
            ) AS grouped
            GROUP BY grouped.session_id, grouped.window_no;

            DELETE FROM message_chunk_fts;
            INSERT INTO message_chunk_fts(chunk_id, session_id, title, summary, content)
            SELECT id, session_id, title, summary, content FROM message_chunks;
        """)
        self._set_schema_version(7)

    @staticmethod
    def _legacy_item_type(key: str) -> str:
        lowered = key.lower()
        profile_hints = (
            "name", "nickname", "location", "city", "language", "timezone",
            "preference", "prefer", "habit", "称呼", "名字", "城市", "地区", "偏好", "习惯",
        )
        return "profile" if any(hint in lowered for hint in profile_hints) else "fact"

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
