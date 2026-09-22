"""Rebuildable conversation archive index and layered deep recall."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Any

from pawmate.core.safety.redaction import redact_text
from pawmate.storage.session_store import SessionStore

from .memory_store import MemoryStore


class HistoryArchive:
    SESSION_SUMMARY_MAX_CHARS = 5000
    CHUNK_CONTENT_MAX_CHARS = 8000

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def record_turn(
        self,
        *,
        session_id: str,
        start_seq: int | None,
        end_seq: int | None,
        user_message: str,
        assistant_response: str,
    ) -> dict[str, Any] | None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        source = SessionStore(db_path=self.store.db_path)
        try:
            source.ensure_session(session_id)
            if start_seq is not None and end_seq is not None:
                messages = source.get_messages_range(
                    session_id,
                    int(start_seq),
                    int(end_seq),
                    limit=120,
                )
            else:
                messages = []
        finally:
            source.close()
        if not messages:
            now = time.time()
            messages = [
                {"seq": start_seq, "role": "user", "content": user_message, "created_at": now},
                {"seq": end_seq, "role": "assistant", "content": assistant_response, "created_at": now},
            ]
        seq_values = [int(item["seq"]) for item in messages if item.get("seq") is not None]
        resolved_start = min(seq_values) if seq_values else int(start_seq or 0)
        resolved_end = max(seq_values) if seq_values else int(end_seq or resolved_start)
        user_text = self._first_role(messages, "user") or str(user_message or "")
        assistant_text = self._last_role(messages, "assistant") or str(assistant_response or "")
        title = self._title(user_text)
        turn_summary = self._turn_summary(user_text, assistant_text)
        content = self._chunk_content(messages)
        chunk_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"pawmate:turn:{session_id}:{resolved_start}:{resolved_end}",
            )
        )
        now = time.time()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO message_chunks ("
                "id, session_id, start_seq, end_seq, chunk_kind, title, summary, content, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'turn', ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, start_seq, end_seq, chunk_kind) DO UPDATE SET "
                "title = excluded.title, summary = excluded.summary, content = excluded.content, "
                "updated_at = excluded.updated_at",
                (
                    chunk_id,
                    session_id,
                    resolved_start,
                    resolved_end,
                    title,
                    turn_summary,
                    content,
                    now,
                    now,
                ),
            )
            conn.execute("DELETE FROM message_chunk_fts WHERE chunk_id = ?", (chunk_id,))
            conn.execute(
                "INSERT INTO message_chunk_fts(chunk_id, session_id, title, summary, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (chunk_id, session_id, title, turn_summary, content),
            )
            summary_row = conn.execute(
                "SELECT title, summary, covered_start_seq, covered_end_seq, created_at "
                "FROM session_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            previous = str(summary_row["summary"] or "") if summary_row else ""
            merged = self._merge_session_summary(previous, turn_summary)
            session_title = str(summary_row["title"] or "") if summary_row else ""
            if not session_title:
                session_title = title
            covered_start = resolved_start
            covered_end = resolved_end
            created_at = now
            if summary_row:
                if summary_row["covered_start_seq"] is not None:
                    covered_start = min(covered_start, int(summary_row["covered_start_seq"]))
                if summary_row["covered_end_seq"] is not None:
                    covered_end = max(covered_end, int(summary_row["covered_end_seq"]))
                created_at = float(summary_row["created_at"] or now)
            topics = self._topics(f"{session_title}\n{merged}")
            conn.execute(
                "INSERT INTO session_summaries ("
                "session_id, title, summary, topics_json, covered_start_seq, covered_end_seq, "
                "summary_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET title = excluded.title, summary = excluded.summary, "
                "topics_json = excluded.topics_json, covered_start_seq = excluded.covered_start_seq, "
                "covered_end_seq = excluded.covered_end_seq, summary_version = excluded.summary_version, "
                "updated_at = excluded.updated_at",
                (
                    session_id,
                    session_title,
                    merged,
                    json.dumps(topics, ensure_ascii=False),
                    covered_start,
                    covered_end,
                    created_at,
                    now,
                ),
            )
            conn.execute("DELETE FROM session_summary_fts WHERE session_id = ?", (session_id,))
            conn.execute(
                "INSERT INTO session_summary_fts(session_id, title, summary, topics) VALUES (?, ?, ?, ?)",
                (session_id, session_title, merged, " ".join(topics)),
            )
        return {
            "chunk_id": chunk_id,
            "session_id": session_id,
            "start_seq": resolved_start,
            "end_seq": resolved_end,
            "title": title,
            "summary": turn_summary,
        }

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit or 8), 50))
        fts_query = self._fts_query(query)
        summary_hits = self._search_summaries(fts_query, max(limit, 10))
        chunk_hits = self._search_chunks(fts_query, max(limit * 2, 16))
        raw_hits = self._search_raw(fts_query, max(limit * 3, 24))
        if not summary_hits and not chunk_hits and not raw_hits:
            raw_hits = self._search_raw_like(query, max(limit * 2, 16))

        session_priority = {
            str(item["session_id"]): rank for rank, item in enumerate(summary_hits)
        }
        merged: dict[str, dict[str, Any]] = {}
        for rank, item in enumerate(chunk_hits):
            key = f"chunk:{item['chunk_id']}"
            item["retrieval"] = "session-summary+chunk" if item["session_id"] in session_priority else "chunk"
            item["score"] = self._rank_score(rank, session_priority.get(item["session_id"]))
            merged[key] = item
        for rank, item in enumerate(raw_hits):
            key = f"message:{item['message_id']}"
            item["retrieval"] = "session-summary+raw" if item["session_id"] in session_priority else "raw"
            item["score"] = self._rank_score(rank, session_priority.get(item["session_id"]))
            merged[key] = item
        if not merged:
            for rank, item in enumerate(summary_hits):
                key = f"session:{item['session_id']}"
                item["retrieval"] = "session-summary"
                item["score"] = self._rank_score(rank, rank)
                merged[key] = item
        results = list(merged.values())
        results.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return results[:limit]

    def open_context(
        self,
        *,
        session_id: str,
        start_seq: int,
        end_seq: int | None = None,
        radius: int = 3,
        limit: int = 40,
    ) -> dict[str, Any]:
        radius = max(0, min(int(radius or 0), 20))
        start = max(0, int(start_seq) - radius)
        end = int(start_seq if end_seq is None else end_seq) + radius
        source = SessionStore(db_path=self.store.db_path)
        try:
            messages = source.get_messages_range(
                str(session_id),
                start,
                end,
                limit=max(1, min(int(limit or 40), 120)),
            )
        finally:
            source.close()
        return {
            "ready": True,
            "session_id": str(session_id),
            "start_seq": start,
            "end_seq": end,
            "messages": messages,
            "error": None,
        }

    def list_sessions(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 30), 100))
        query = str(query or "").strip()
        if query:
            session_ids = list(
                dict.fromkeys(str(hit["session_id"]) for hit in self.search(query, limit=200))
            )
            total = len(session_ids)
            page_ids = session_ids[(page - 1) * page_size : page * page_size]
            if page_ids:
                placeholders = ",".join("?" for _ in page_ids)
                rows = self.store._connect().execute(
                    "SELECT s.id AS session_id, s.title AS source_title, s.created_at, s.updated_at, "
                    "s.message_count, ss.title, ss.summary, ss.covered_start_seq, ss.covered_end_seq "
                    f"FROM sessions s LEFT JOIN session_summaries ss ON ss.session_id = s.id "
                    f"WHERE s.id IN ({placeholders})",
                    page_ids,
                ).fetchall()
                by_id = {str(row["session_id"]): self._session_row(row) for row in rows}
                items = [by_id[item_id] for item_id in page_ids if item_id in by_id]
            else:
                items = []
        else:
            total_row = self.store._connect().execute("SELECT COUNT(*) AS cnt FROM sessions").fetchone()
            total = int(total_row["cnt"] if total_row else 0)
            rows = self.store._connect().execute(
                "SELECT s.id AS session_id, s.title AS source_title, s.created_at, s.updated_at, "
                "s.message_count, ss.title, ss.summary, ss.covered_start_seq, ss.covered_end_seq "
                "FROM sessions s LEFT JOIN session_summaries ss ON ss.session_id = s.id "
                "ORDER BY s.updated_at DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
            items = [self._session_row(row) for row in rows]
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

    def stats(self) -> dict[str, int]:
        conn = self.store._connect()
        return {
            "sessions": self._count(conn, "sessions"),
            "messages": self._count(conn, "messages"),
            "summaries": self._count(conn, "session_summaries"),
            "chunks": self._count(conn, "message_chunks"),
            "indexed_messages": self._count(conn, "message_archive_fts"),
        }

    def _search_summaries(self, fts_query: str, limit: int) -> list[dict[str, Any]]:
        try:
            rows = self.store._connect().execute(
                "SELECT f.session_id, f.title, f.summary, s.covered_start_seq, s.covered_end_seq, "
                "s.updated_at FROM session_summary_fts f JOIN session_summaries s "
                "ON s.session_id = f.session_id WHERE session_summary_fts MATCH ? "
                "ORDER BY bm25(session_summary_fts) LIMIT ?",
                (fts_query, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                "hit_id": f"session:{row['session_id']}",
                "kind": "session",
                "session_id": str(row["session_id"]),
                "start_seq": row["covered_start_seq"],
                "end_seq": row["covered_end_seq"],
                "title": str(row["title"] or ""),
                "snippet": self._clip(str(row["summary"] or ""), 500),
                "created_at": row["updated_at"],
            }
            for row in rows
        ]

    def _search_chunks(self, fts_query: str, limit: int) -> list[dict[str, Any]]:
        try:
            rows = self.store._connect().execute(
                "SELECT f.chunk_id, f.session_id, f.title, f.summary, c.start_seq, c.end_seq, c.created_at "
                "FROM message_chunk_fts f JOIN message_chunks c ON c.id = f.chunk_id "
                "WHERE message_chunk_fts MATCH ? ORDER BY bm25(message_chunk_fts) LIMIT ?",
                (fts_query, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                "hit_id": f"chunk:{row['chunk_id']}",
                "kind": "chunk",
                "chunk_id": str(row["chunk_id"]),
                "session_id": str(row["session_id"]),
                "start_seq": int(row["start_seq"]),
                "end_seq": int(row["end_seq"]),
                "title": str(row["title"] or ""),
                "snippet": self._clip(str(row["summary"] or ""), 500),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _search_raw(self, fts_query: str, limit: int) -> list[dict[str, Any]]:
        try:
            rows = self.store._connect().execute(
                "SELECT message_id, session_id, seq, role, content, created_at "
                "FROM message_archive_fts WHERE message_archive_fts MATCH ? "
                "ORDER BY bm25(message_archive_fts) LIMIT ?",
                (fts_query, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._raw_hit(row) for row in rows]

    def _search_raw_like(self, query: str, limit: int) -> list[dict[str, Any]]:
        rows = self.store._connect().execute(
            "SELECT id AS message_id, session_id, seq, role, content, created_at "
            "FROM messages WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", int(limit)),
        ).fetchall()
        return [self._raw_hit(row) for row in rows]

    def _raw_hit(self, row) -> dict[str, Any]:
        return {
            "hit_id": f"message:{row['message_id']}",
            "kind": "message",
            "message_id": int(row["message_id"]),
            "session_id": str(row["session_id"]),
            "start_seq": int(row["seq"]),
            "end_seq": int(row["seq"]),
            "role": str(row["role"]),
            "title": f"{row['role']} · message {row['seq']}",
            "snippet": self._clip(self._decode_content(row["content"]), 500),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _rank_score(rank: int, session_rank: int | None) -> float:
        score = 1.0 / (1.0 + rank * 0.08)
        if session_rank is not None:
            score += 0.15 / (1.0 + session_rank)
        return min(1.0, score)

    @staticmethod
    def _fts_query(query: str) -> str:
        lowered = query.lower()
        lowered = re.sub(
            r"(请|帮我|一下|之前|以前|那个|我们|你还|记得|召回|找找|查查|关于|聊过|说过)",
            " ",
            lowered,
        )
        english = re.findall(r"[a-z0-9_./-]{3,}", lowered)
        chinese_runs = re.findall(r"[\u4e00-\u9fff]{3,}", lowered)
        tokens: list[str] = []
        tokens.extend(english[:8])
        for run in chinese_runs:
            if len(run) <= 6:
                tokens.append(run)
            else:
                tokens.extend(run[index : index + 3] for index in range(0, len(run) - 2, 2))
            if len(tokens) >= 12:
                break
        if not tokens:
            tokens = [query]
        unique = list(dict.fromkeys(token.strip() for token in tokens if token.strip()))[:12]
        return " OR ".join('"' + token.replace('"', '""') + '"' for token in unique)

    @staticmethod
    def _turn_summary(user_text: str, assistant_text: str) -> str:
        user = HistoryArchive._clip_one_line(redact_text(user_text), 340)
        assistant = HistoryArchive._clip_one_line(redact_text(assistant_text), 520)
        return f"用户：{user}\n助手结果：{assistant}".strip()

    @staticmethod
    def _merge_session_summary(previous: str, turn_summary: str) -> str:
        blocks = [block.strip() for block in previous.split("\n---\n") if block.strip()]
        if not blocks or blocks[-1] != turn_summary:
            blocks.append(turn_summary)
        while len("\n---\n".join(blocks)) > HistoryArchive.SESSION_SUMMARY_MAX_CHARS and len(blocks) > 1:
            blocks.pop(0)
        return "\n---\n".join(blocks)[-HistoryArchive.SESSION_SUMMARY_MAX_CHARS :]

    @staticmethod
    def _chunk_content(messages: list[dict[str, Any]]) -> str:
        lines = []
        for item in messages:
            text = HistoryArchive._message_text(item.get("content"))
            lines.append(f"[{item.get('seq')}] {item.get('role')}: {redact_text(text)}")
        return "\n".join(lines)[: HistoryArchive.CHUNK_CONTENT_MAX_CHARS]

    @staticmethod
    def _first_role(messages: list[dict[str, Any]], role: str) -> str:
        for item in messages:
            if item.get("role") == role:
                return HistoryArchive._message_text(item.get("content"))
        return ""

    @staticmethod
    def _last_role(messages: list[dict[str, Any]], role: str) -> str:
        for item in reversed(messages):
            if item.get("role") == role:
                return HistoryArchive._message_text(item.get("content"))
        return ""

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, dict):
            return str(content.get("text") or json.dumps(content, ensure_ascii=False, default=str))
        if isinstance(content, list):
            return json.dumps(content, ensure_ascii=False, default=str)
        return str(content or "")

    @staticmethod
    def _decode_content(content: Any) -> str:
        try:
            return HistoryArchive._message_text(json.loads(str(content)))
        except Exception:
            return str(content or "")

    @staticmethod
    def _title(text: str) -> str:
        one = HistoryArchive._clip_one_line(text, 60)
        for separator in ("。", "？", "?", "！", "!", "\n"):
            if separator in one:
                one = one.split(separator, 1)[0]
        return one.strip(" ：:，,") or "对话摘要"

    @staticmethod
    def _topics(text: str) -> list[str]:
        english = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
        chinese = re.findall(r"[\u4e00-\u9fff]{3,8}", text)
        stop = {"用户", "助手", "结果", "这个", "那个", "可以", "已经"}
        return [item for item in dict.fromkeys([*english, *chinese]) if item not in stop][:12]

    @staticmethod
    def _session_row(row) -> dict[str, Any]:
        title = str(row["title"] or row["source_title"] or "对话")
        return {
            "session_id": str(row["session_id"]),
            "title": title,
            "summary": str(row["summary"] or ""),
            "message_count": int(row["message_count"] or 0),
            "covered_start_seq": row["covered_start_seq"],
            "covered_end_seq": row["covered_end_seq"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _count(conn, table: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()
        return int(row["cnt"] if row else 0)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = str(text or "")
        return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"

    @staticmethod
    def _clip_one_line(text: str, limit: int) -> str:
        return HistoryArchive._clip(re.sub(r"\s+", " ", str(text or "")).strip(), limit)
