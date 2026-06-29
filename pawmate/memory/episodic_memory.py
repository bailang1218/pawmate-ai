"""
EpisodicMemory — searchable short-term notes via SQLite FTS5 (trigram).

NOT automatically injected into system prompt. AI must actively call
search_memory() or list_notes() to retrieve.

注：title 存放在 metadata(UNINDEXED) 的 JSON 里，避免改 FTS5 表结构。
"""
import json
import sqlite3
import time
import logging
from typing import Any, Dict, List, Optional

from pawmate.memory.memory_store import MemoryStore
from pawmate.memory.embedding import HashingEmbeddingProvider

logger = logging.getLogger("pawmate.memory.episodic")


class EpisodicMemory:
    def __init__(self, store: MemoryStore):
        self.store = store
        self._embedder = HashingEmbeddingProvider()

    @staticmethod
    def _meta(meta_str: str) -> Dict[str, Any]:
        try:
            m = json.loads(meta_str) if meta_str else {}
            return m if isinstance(m, dict) else {}
        except Exception:
            return {}

    def note(
        self,
        content: str,
        tags: Optional[List[str]] = None,
        title: str = "",
        *,
        session_id: str = "",
        start_seq: Optional[int] = None,
        end_seq: Optional[int] = None,
        source_started_at: Optional[float] = None,
        source_ended_at: Optional[float] = None,
        summary_kind: str = "",
    ) -> int:
        """Save a short-term note, optionally tagged + titled. Returns row id."""
        content = content.strip()
        if not content:
            raise ValueError("content cannot be empty")
        tags_str = " ".join(tags) if tags else ""
        title = (title or "").strip()
        metadata: Dict[str, Any] = {"title": title}
        if session_id:
            metadata["session_id"] = session_id
        if start_seq is not None:
            metadata["start_seq"] = int(start_seq)
        if end_seq is not None:
            metadata["end_seq"] = int(end_seq)
        if source_started_at is not None:
            metadata["source_started_at"] = float(source_started_at)
        if source_ended_at is not None:
            metadata["source_ended_at"] = float(source_ended_at)
        if summary_kind:
            metadata["summary_kind"] = summary_kind
        now = time.time()
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO episodic_memory(content, tags, metadata, created_at) "
                "VALUES(?, ?, ?, ?)",
                (content, tags_str, json.dumps(metadata, ensure_ascii=False), now),
            )
            rowid = cur.lastrowid
        self._upsert_embedding(rowid, content, tags_str, title)
        return rowid

    def _row_to_dict(self, row, *, include_score: bool = False) -> Dict[str, Any]:
        meta = self._meta(row["metadata"])
        item = {
            "rowid": row["rowid"],
            "title": meta.get("title", ""),
            "content": row["content"],
            "tags": row["tags"] or "",
            "created_at": row["created_at"],
            "source_session_id": meta.get("session_id", ""),
            "source_start_seq": meta.get("start_seq"),
            "source_end_seq": meta.get("end_seq"),
            "source_started_at": meta.get("source_started_at"),
            "source_ended_at": meta.get("source_ended_at"),
            "summary_kind": meta.get("summary_kind", ""),
        }
        if include_score:
            item["score"] = row["score"]
        return item

    def search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """Hybrid FTS + local vector search, returns top-k results."""
        query = query.strip()
        if not query:
            return []
        fts_items: list[Dict[str, Any]] = []
        try:
            rows = self._search_rows(query, k)
            fts_items = [self._row_to_dict(r, include_score=True) for r in rows]
        except sqlite3.OperationalError:
            try:
                rows = self._search_rows(self._quote_query(query), k)
                fts_items = [self._row_to_dict(r, include_score=True) for r in rows]
            except sqlite3.OperationalError:
                fts_items = []
        return self._merge_vector_results(query, fts_items, k)

    def _search_rows(self, query: str, k: int):
        return self.store._connect().execute(
            "SELECT rowid, content, tags, metadata, created_at, rank AS score "
            "FROM episodic_memory "
            "WHERE episodic_memory MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, k),
        ).fetchall()

    @staticmethod
    def _quote_query(query: str) -> str:
        return '"' + query.replace('"', '""') + '"'

    def list_recent(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return the n most recent notes (newest first)."""
        rows = self.store._connect().execute(
            "SELECT rowid, content, tags, metadata, created_at FROM episodic_memory "
            "ORDER BY created_at DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, rowid: int) -> Optional[Dict[str, Any]]:
        row = self.store._connect().execute(
            "SELECT rowid, content, tags, metadata, created_at FROM episodic_memory "
            "WHERE rowid = ?",
            (int(rowid),),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def update(self, rowid: int, content: str) -> bool:
        """Update a note's content by rowid."""
        content = content.strip()
        if not content:
            raise ValueError("content cannot be empty")
        with self.store.transaction() as conn:
            cur = conn.execute(
                "UPDATE episodic_memory SET content = ? WHERE rowid = ?",
                (content, int(rowid)),
            )
            changed = cur.rowcount > 0
        if changed:
            item = self.get(rowid)
            self._upsert_embedding(
                int(rowid),
                content,
                str(item.get("tags") or "") if item else "",
                str(item.get("title") or "") if item else "",
            )
        return changed

    def delete(self, rowid: int) -> bool:
        """Delete a note by rowid."""
        with self.store.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM episodic_memory WHERE rowid = ?",
                (int(rowid),),
            )
            changed = cur.rowcount > 0
        if changed:
            self.store.delete_embedding(memory_type="episodic", memory_rowid=int(rowid))
        return changed

    def _upsert_embedding(self, rowid: int, content: str, tags: str = "", title: str = "") -> None:
        text = "\n".join(part for part in [title, tags, content] if part)
        vector = self._embedder.embed(text)
        self.store.upsert_embedding(
            memory_type="episodic",
            memory_rowid=int(rowid),
            embedding_model=self._embedder.model_name,
            vector=vector,
        )

    def _merge_vector_results(
        self,
        query: str,
        fts_items: list[Dict[str, Any]],
        k: int,
    ) -> list[Dict[str, Any]]:
        merged: dict[int, Dict[str, Any]] = {}
        for rank, item in enumerate(fts_items):
            rowid = int(item.get("rowid") or 0)
            if not rowid:
                continue
            item = dict(item)
            item["retrieval"] = "fts"
            item["hybrid_score"] = 1.0 - min(rank, 10) * 0.05
            merged[rowid] = item

        query_vector = self._embedder.embed(query)
        vector_hits = self.store.search_embeddings(
            memory_type="episodic",
            embedding_model=self._embedder.model_name,
            query_vector=query_vector,
            k=max(k, 8),
        )
        for rowid, score in vector_hits:
            item = merged.get(rowid)
            if item is None:
                loaded = self.get(rowid)
                if loaded is None:
                    continue
                item = dict(loaded)
                item["retrieval"] = "vector"
                merged[rowid] = item
            elif item.get("retrieval") == "fts":
                item["retrieval"] = "hybrid"
            item["vector_score"] = score
            item["hybrid_score"] = float(item.get("hybrid_score", 0.0)) + score

        items = list(merged.values())
        items.sort(key=lambda item: float(item.get("hybrid_score", 0.0)), reverse=True)
        return items[: max(1, int(k))]
