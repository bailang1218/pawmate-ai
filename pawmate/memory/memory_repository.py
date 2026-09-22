"""Unified long-term memory CRUD, audit, and hybrid retrieval."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from typing import Any

from .embedding import EmbeddingProvider, cosine_similarity, create_embedding_provider
from .memory_store import MemoryStore


VALID_ITEM_TYPES = {"profile", "fact", "event", "lesson"}
VALID_STATUSES = {"active", "pending_review", "superseded", "deleted"}


class MemoryRepository:
    def __init__(
        self,
        store: MemoryStore,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.store = store
        self.embedding = embedding_provider or create_embedding_provider()

    def embedding_status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.embedding.enabled),
            "model": self.embedding.model_name,
            "reason": self.embedding.reason,
        }

    def create(
        self,
        *,
        item_type: str,
        content: str,
        subject: str = "user",
        predicate: str = "",
        scope_type: str = "global",
        scope_id: str = "local-default",
        importance: float = 0.5,
        confidence: float = 1.0,
        status: str = "active",
        pinned: bool = False,
        source_kind: str = "manual",
        source_session_id: str = "",
        source_start_seq: int | None = None,
        source_end_seq: int | None = None,
        valid_from: float | None = None,
        valid_until: float | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "user",
        reason: str = "explicit_memory_write",
    ) -> dict[str, Any]:
        item_type = self._item_type(item_type)
        status = self._status(status)
        content = str(content or "").strip()
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        now = time.time()
        content_hash = self._content_hash(subject, predicate, content)
        item_id = str(uuid.uuid4())
        payload = json.dumps(metadata or {}, ensure_ascii=False, default=str)
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO memory_items ("
                    "id, item_type, scope_type, scope_id, subject, predicate, content, "
                    "importance, confidence, status, pinned, source_kind, "
                    "source_session_id, source_start_seq, source_end_seq, valid_from, valid_until, "
                    "content_hash, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        item_type,
                        scope_type,
                        scope_id,
                        subject,
                        predicate,
                        content,
                        self._unit_float(importance, 0.5),
                        self._unit_float(confidence, 1.0),
                        status,
                        int(bool(pinned)),
                        str(source_kind or "manual"),
                        str(source_session_id or ""),
                        source_start_seq,
                        source_end_seq,
                        valid_from,
                        valid_until,
                        content_hash,
                        payload,
                        now,
                        now,
                    ),
                )
                if status != "deleted":
                    self._insert_fts(conn, item_id, item_type, scope_id, subject, predicate, content)
                self._audit(conn, item_id, "create", actor, reason, metadata or {})
        except sqlite3.IntegrityError as exc:
            existing = self.store._connect().execute(
                "SELECT * FROM memory_items WHERE scope_type = ? AND scope_id = ? "
                "AND item_type = ? AND content_hash = ? "
                "AND status IN ('active', 'pending_review') LIMIT 1",
                (scope_type, scope_id, item_type, content_hash),
            ).fetchone()
            if existing is not None:
                return self._row(existing)
            raise exc
        self._update_embedding(item_id, self._embedding_text(subject, predicate, content), content_hash)
        return self.get(item_id) or {"id": item_id}

    def update(
        self,
        item_id: str,
        *,
        content: str | None = None,
        item_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        pinned: bool | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        source_kind: str | None = None,
        source_session_id: str | None = None,
        source_start_seq: int | None = None,
        source_end_seq: int | None = None,
        actor: str = "user",
        reason: str = "memory_updated",
    ) -> dict[str, Any]:
        current = self.get(item_id)
        if current is None:
            raise KeyError(f"memory not found: {item_id}")
        next_type = self._item_type(item_type or current["item_type"])
        next_status = self._status(status or current["status"])
        next_content = str(current["content"] if content is None else content).strip()
        next_subject = str(current["subject"] if subject is None else subject).strip()
        next_predicate = str(current["predicate"] if predicate is None else predicate).strip()
        next_source_kind = str(
            current["source_kind"] if source_kind is None else source_kind
        ).strip()
        next_source_session_id = str(
            current["source_session_id"]
            if source_session_id is None
            else source_session_id
        ).strip()
        next_source_start_seq = (
            current["source_start_seq"]
            if source_start_seq is None
            else int(source_start_seq)
        )
        next_source_end_seq = (
            current["source_end_seq"]
            if source_end_seq is None
            else int(source_end_seq)
        )
        if not next_content:
            raise ValueError("memory content cannot be empty")
        next_hash = self._content_hash(next_subject, next_predicate, next_content)
        next_meta = current.get("metadata", {}) if metadata is None else metadata
        now = time.time()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE memory_items SET item_type = ?, subject = ?, predicate = ?, content = ?, "
                "importance = ?, confidence = ?, status = ?, pinned = ?, content_hash = ?, "
                "metadata_json = ?, source_kind = ?, source_session_id = ?, "
                "source_start_seq = ?, source_end_seq = ?, updated_at = ? WHERE id = ?",
                (
                    next_type,
                    next_subject,
                    next_predicate,
                    next_content,
                    current["importance"] if importance is None else self._unit_float(importance, 0.5),
                    current["confidence"] if confidence is None else self._unit_float(confidence, 1.0),
                    next_status,
                    int(current["pinned"] if pinned is None else bool(pinned)),
                    next_hash,
                    json.dumps(next_meta or {}, ensure_ascii=False, default=str),
                    next_source_kind,
                    next_source_session_id,
                    next_source_start_seq,
                    next_source_end_seq,
                    now,
                    item_id,
                ),
            )
            conn.execute("DELETE FROM memory_item_fts WHERE item_id = ?", (item_id,))
            if next_status != "deleted":
                self._insert_fts(
                    conn,
                    item_id,
                    next_type,
                    current["scope_id"],
                    next_subject,
                    next_predicate,
                    next_content,
                )
            self._audit(conn, item_id, "update", actor, reason, next_meta or {})
        if next_status == "deleted":
            self.store._connect().execute(
                "DELETE FROM semantic_embeddings WHERE object_type = 'memory_item' AND object_id = ?",
                (item_id,),
            )
        else:
            self._update_embedding(
                item_id,
                self._embedding_text(next_subject, next_predicate, next_content),
                next_hash,
            )
        return self.get(item_id) or {"id": item_id}

    def soft_delete(self, item_id: str, *, actor: str = "user", reason: str = "user_deleted") -> bool:
        if self.get(item_id) is None:
            return False
        self.update(item_id, status="deleted", actor=actor, reason=reason)
        return True

    def restore(self, item_id: str, *, actor: str = "user") -> bool:
        item = self.get(item_id)
        if item is None or item["status"] != "deleted":
            return False
        self.update(item_id, status="active", actor=actor, reason="user_restored")
        return True

    def get(self, item_id: str) -> dict[str, Any] | None:
        row = self.store._connect().execute(
            "SELECT * FROM memory_items WHERE id = ?",
            (str(item_id),),
        ).fetchone()
        return self._row(row) if row else None

    def find_by_predicate(self, predicate: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        query = "SELECT * FROM memory_items WHERE predicate = ?"
        params: list[Any] = [str(predicate)]
        if not include_deleted:
            query += " AND status != 'deleted'"
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self.store._connect().execute(query, params).fetchone()
        return self._row(row) if row else None

    def list_page(
        self,
        *,
        item_type: str = "",
        status: str = "active",
        query: str = "",
        page: int = 1,
        page_size: int = 30,
        scope_id: str = "local-default",
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 30), 100))
        item_type = str(item_type or "").strip()
        status = str(status or "active").strip()
        query = str(query or "").strip()
        if query:
            ranked = self.search(
                query,
                item_types=[item_type] if item_type else None,
                statuses=[status] if status else None,
                limit=1000,
                scope_id=scope_id,
            )
            total = len(ranked)
            start = (page - 1) * page_size
            items = ranked[start : start + page_size]
        else:
            where = ["scope_id = ?"]
            params: list[Any] = [scope_id]
            if item_type:
                where.append("item_type = ?")
                params.append(self._item_type(item_type))
            if status:
                where.append("status = ?")
                params.append(self._status(status))
            clause = " AND ".join(where)
            total_row = self.store._connect().execute(
                f"SELECT COUNT(*) AS cnt FROM memory_items WHERE {clause}",
                params,
            ).fetchone()
            total = int(total_row["cnt"] if total_row else 0)
            rows = self.store._connect().execute(
                f"SELECT * FROM memory_items WHERE {clause} "
                "ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            items = [self._row(row) for row in rows]
        return {
            "ready": True,
            "status": "ready",
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": page * page_size < total,
            "error": None,
        }

    def search(
        self,
        query: str,
        *,
        item_types: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 8,
        scope_id: str = "local-default",
    ) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit or 8), 1000))
        allowed_types = {self._item_type(value) for value in item_types or []}
        allowed_statuses = {self._status(value) for value in statuses or ["active"]}
        fts_ids = self._fts_ids(query, max(limit * 4, 40))
        vector_scores = self._vector_scores(query, max(limit * 4, 40))
        candidate_ids = list(dict.fromkeys([*fts_ids, *vector_scores.keys()]))
        if not candidate_ids:
            candidate_ids = self._like_ids(query, max(limit * 4, 40))
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.store._connect().execute(
            f"SELECT * FROM memory_items WHERE id IN ({placeholders})",
            candidate_ids,
        ).fetchall()
        by_id = {str(row["id"]): self._row(row) for row in rows}
        fts_rank = {item_id: rank for rank, item_id in enumerate(fts_ids)}
        results: list[dict[str, Any]] = []
        for item_id in candidate_ids:
            item = by_id.get(item_id)
            if item is None or item["scope_id"] != scope_id:
                continue
            if allowed_types and item["item_type"] not in allowed_types:
                continue
            if item["status"] not in allowed_statuses:
                continue
            rrf = 0.0
            modes: list[str] = []
            if item_id in fts_rank:
                rrf += 1.0 / (60.0 + fts_rank[item_id] + 1.0)
                modes.append("fts")
            if item_id in vector_scores:
                rrf += max(0.0, vector_scores[item_id]) / 60.0
                modes.append("semantic")
            item["retrieval"] = "+".join(modes) or "substring"
            item["vector_score"] = vector_scores.get(item_id)
            item["hybrid_score"] = min(1.0, rrf * 30.0) if rrf else 0.45
            results.append(item)
        results.sort(
            key=lambda item: (
                float(item.get("hybrid_score") or 0.0),
                bool(item.get("pinned")),
                float(item.get("importance") or 0.0),
                float(item.get("updated_at") or 0.0),
            ),
            reverse=True,
        )
        return results[:limit]

    def counts(self) -> dict[str, int]:
        rows = self.store._connect().execute(
            "SELECT item_type, status, COUNT(*) AS cnt FROM memory_items GROUP BY item_type, status"
        ).fetchall()
        result = {"profile": 0, "fact": 0, "event": 0, "lesson": 0, "trash": 0, "review": 0}
        for row in rows:
            if row["status"] == "active":
                result[str(row["item_type"])] += int(row["cnt"])
            elif row["status"] == "deleted":
                result["trash"] += int(row["cnt"])
            elif row["status"] == "pending_review":
                result["review"] += int(row["cnt"])
        return result

    def _fts_ids(self, query: str, limit: int) -> list[str]:
        try:
            rows = self.store._connect().execute(
                "SELECT item_id FROM memory_item_fts WHERE memory_item_fts MATCH ? "
                "ORDER BY bm25(memory_item_fts) LIMIT ?",
                (self._fts_query(query), int(limit)),
            ).fetchall()
            return [str(row["item_id"]) for row in rows]
        except sqlite3.OperationalError:
            return []

    def _like_ids(self, query: str, limit: int) -> list[str]:
        pattern = f"%{query}%"
        rows = self.store._connect().execute(
            "SELECT id FROM memory_items WHERE status != 'deleted' "
            "AND (content LIKE ? OR subject LIKE ? OR predicate LIKE ?) "
            "ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?",
            (pattern, pattern, pattern, int(limit)),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def _vector_scores(self, query: str, limit: int) -> dict[str, float]:
        if not self.embedding.enabled:
            return {}
        try:
            query_vector = self.embedding.embed(query)
        except Exception:
            return {}
        if not query_vector:
            return {}
        rows = self.store._connect().execute(
            "SELECT object_id, vector_json FROM semantic_embeddings "
            "WHERE object_type = 'memory_item' AND embedding_model = ?",
            (self.embedding.model_name,),
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            try:
                vector = json.loads(row["vector_json"])
            except Exception:
                continue
            score = cosine_similarity(query_vector, vector)
            if score > 0:
                scored.append((str(row["object_id"]), float(score)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return dict(scored[:limit])

    def _update_embedding(self, item_id: str, text: str, content_hash: str) -> None:
        if not self.embedding.enabled:
            return
        try:
            vector = self.embedding.embed(text)
            if not vector:
                return
            self.store._connect().execute(
                "INSERT OR REPLACE INTO semantic_embeddings "
                "(object_type, object_id, embedding_model, dim, vector_json, content_hash, updated_at) "
                "VALUES ('memory_item', ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    self.embedding.model_name,
                    len(vector),
                    json.dumps(vector, separators=(",", ":")),
                    content_hash,
                    time.time(),
                ),
            )
        except Exception:
            # A memory write must remain durable even if an optional embedding
            # service is offline. Status exposes that semantic retrieval is best effort.
            return

    @staticmethod
    def _insert_fts(conn, item_id, item_type, scope_id, subject, predicate, content) -> None:
        conn.execute(
            "INSERT INTO memory_item_fts(item_id, item_type, scope_id, subject, predicate, content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, item_type, scope_id, subject, predicate, content),
        )

    @staticmethod
    def _audit(conn, item_id, action, actor, reason, payload) -> None:
        conn.execute(
            "INSERT INTO memory_audit_log(item_id, action, actor, reason, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                item_id,
                action,
                actor,
                reason,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                time.time(),
            ),
        )

    @staticmethod
    def _content_hash(subject: str, predicate: str, content: str) -> str:
        canonical = "\n".join([subject.strip().lower(), predicate.strip().lower(), content.strip()])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _embedding_text(subject: str, predicate: str, content: str) -> str:
        return "\n".join(part for part in [subject, predicate, content] if part)

    @staticmethod
    def _fts_query(query: str) -> str:
        lowered = str(query or "").lower()
        lowered = re.sub(
            r"(请|帮我|一下|现在|之前|以前|那个|关于|记得|召回|查找|搜索|什么)",
            " ",
            lowered,
        )
        tokens = re.findall(r"[a-z0-9_./-]{3,}|[\u4e00-\u9fff]{3,8}", lowered)
        if not tokens:
            tokens = [query]
        return " OR ".join(
            '"' + token.replace('"', '""') + '"'
            for token in list(dict.fromkeys(tokens))[:10]
            if token
        )

    @staticmethod
    def _item_type(value: str) -> str:
        value = str(value or "").strip().lower()
        if value not in VALID_ITEM_TYPES:
            raise ValueError(f"unsupported memory type: {value}")
        return value

    @staticmethod
    def _status(value: str) -> str:
        value = str(value or "").strip().lower()
        if value not in VALID_STATUSES:
            raise ValueError(f"unsupported memory status: {value}")
        return value

    @staticmethod
    def _unit_float(value: float, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _row(row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except Exception:
            item["metadata"] = {}
            item.pop("metadata_json", None)
        item["pinned"] = bool(item.get("pinned"))
        return item
