"""
CoreMemory — permanent key-value memory with soft-delete and AI protection.

Key rules:
- User-deleted (suppressed) memories cannot be auto-resurrected by AI.
- User-manual memories cannot be silently overwritten by AI.
- Pinned memories cannot be silently overwritten by AI.
- Hard limits: max_entries (default 10), max_chars (default 800).
"""
from __future__ import annotations

import time
import logging
import os
from typing import Any, Dict, List, Optional

from pawmate.memory.memory_store import MemoryStore

logger = logging.getLogger("pawmate.memory.core")


class CoreMemoryFull(Exception):
    """Core memory is full — AI must delete something first."""


class CoreMemory:
    def __init__(
        self,
        store: MemoryStore,
        max_entries: int | None = None,
        max_chars: int | None = None,
    ):
        self.store = store
        self.max_entries = self._resolve_limit("PAWMATE_CORE_MEMORY_MAX_ENTRIES", max_entries, 10)
        self.max_chars = self._resolve_limit("PAWMATE_CORE_MEMORY_MAX_CHARS", max_chars, 800)

    # ── Public: AI-initiated write ────────────────────────────

    def remember(self, key: str, value: str, *, source: str = "ai") -> Dict[str, Any]:
        """AI calls this to store a memory. Protected by source rules."""
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ValueError("key and value cannot be empty")

        now = time.time()
        conn = self.store._connect()

        existing = conn.execute(
            "SELECT key, source, status, pinned FROM core_memory WHERE key = ?",
            (key,),
        ).fetchone()

        # Rule 1: suppressed by user → AI cannot revive
        if existing and existing["status"] == "suppressed" and source == "ai":
            return {"key": key, "status": "rejected", "reason": "suppressed_by_user"}

        # Rule 2 & 3: manual or pinned → AI cannot overwrite
        if source == "ai" and existing and (
            existing["source"] == "manual" or existing["pinned"]
        ):
            return {"key": key, "status": "rejected", "reason": "protected"}

        return self._upsert(key, value, source=source, pinned=False, now=now)

    def set(self, key: str, value: str) -> Dict[str, Any]:
        """Backward-compatible alias for manual memory writes."""
        return self.create_manual(key, value)

    def forget(self, key: str, *, source: str = "ai") -> bool:
        """AI calls this to remove a memory. Rejects if manual/pinned."""
        conn = self.store._connect()
        existing = conn.execute(
            "SELECT source, pinned FROM core_memory WHERE key = ? AND status = 'active'",
            (key,),
        ).fetchone()
        if not existing:
            return False
        if source == "ai" and (existing["source"] == "manual" or existing["pinned"]):
            return False
        conn.execute(
            "UPDATE core_memory SET status = 'suppressed', deleted_at = ?, updated_at = ? WHERE key = ?",
            (time.time(), time.time(), key),
        )
        return True

    # ── Public: UI CRUD ───────────────────────────────────────

    def get_all(self, include_suppressed: bool = False) -> List[Dict[str, Any]]:
        conn = self.store._connect()
        if include_suppressed:
            rows = conn.execute(
                "SELECT key, value, source, status, pinned, updated_at "
                "FROM core_memory ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, value, source, status, pinned, updated_at "
                "FROM core_memory WHERE status = 'active' ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_manual(self, key: str, value: str, pinned: bool = False) -> Dict[str, Any]:
        """User manually adds a memory. Forces source=manual, status=active."""
        return self._upsert(key.strip(), value.strip(), source="manual", pinned=pinned, status="active")

    def update_manual(self, key: str, value: str, pinned: Optional[bool] = None) -> Dict[str, Any]:
        """User manually edits a memory. Forces source=manual, status=active."""
        conn = self.store._connect()
        existing = conn.execute(
            "SELECT key, pinned FROM core_memory WHERE key = ?", (key,),
        ).fetchone()
        if not existing:
            raise KeyError(f"Memory key not found: {key}")

        p = pinned if pinned is not None else bool(existing["pinned"])
        now = time.time()
        conn.execute(
            "UPDATE core_memory SET value = ?, source = 'manual', status = 'active', "
            "pinned = ?, deleted_at = NULL, updated_at = ? WHERE key = ?",
            (value.strip(), int(p), now, key),
        )
        return {"key": key, "value": value, "status": "updated", "source": "manual"}

    def suppress(self, key: str) -> bool:
        """User deletes a memory. Soft-delete: status = suppressed."""
        conn = self.store._connect()
        now = time.time()
        cur = conn.execute(
            "UPDATE core_memory SET status = 'suppressed', deleted_at = ?, updated_at = ? "
            "WHERE key = ? AND status = 'active'",
            (now, now, key),
        )
        return cur.rowcount > 0

    def delete(self, key: str) -> bool:
        """Backward-compatible alias for user suppression."""
        return self.suppress(key)

    def restore(self, key: str) -> bool:
        """User restores a suppressed memory."""
        conn = self.store._connect()
        cur = conn.execute(
            "UPDATE core_memory SET status = 'active', deleted_at = NULL, "
            "source = 'manual', updated_at = ? WHERE key = ? AND status = 'suppressed'",
            (time.time(), key),
        )
        return cur.rowcount > 0

    def toggle_pin(self, key: str) -> bool:
        conn = self.store._connect()
        existing = conn.execute(
            "SELECT pinned FROM core_memory WHERE key = ? AND status = 'active'",
            (key,),
        ).fetchone()
        if not existing:
            return False
        new_val = 0 if existing["pinned"] else 1
        conn.execute(
            "UPDATE core_memory SET pinned = ? WHERE key = ?", (new_val, key),
        )
        return True

    # ── Prompt injection ──────────────────────────────────────

    def list_all(self) -> List[Dict[str, Any]]:
        """Active-only entries for prompt. (backward compat)"""
        return self.get_all(include_suppressed=False)

    def format_for_prompt(self) -> str:
        entries = self.list_all()
        if not entries:
            return ""
        lines = ["## Long-term memory about the user (known without querying)"]
        for e in entries:
            lines.append(f"- {e['key']}: {e['value']}")
        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────

    def _upsert(self, key, value, source, pinned, status="active", now=None):
        if now is None:
            now = time.time()
        conn = self.store._connect()
        existing = conn.execute("SELECT key FROM core_memory WHERE key = ?", (key,)).fetchone()

        value = self._fit_value_to_char_limit(str(key), str(value))
        new_entry_chars = len(str(key)) + len(str(value))
        self._evict_until_room(conn, key=str(key), new_entry_chars=new_entry_chars, replacing=existing is not None)

        conn.execute(
            "INSERT OR REPLACE INTO core_memory "
            "(key, value, source, status, pinned, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, value, source, status, int(pinned), now, now),
        )
        return {"key": key, "value": value, "status": "stored", "source": source}

    @staticmethod
    def _resolve_limit(env_name: str, explicit: int | None, default: int) -> int:
        raw = explicit if explicit is not None else os.environ.get(env_name, default)
        try:
            return max(1, int(raw))
        except Exception:
            return default

    def _fit_value_to_char_limit(self, key: str, value: str) -> str:
        max_value_chars = max(1, self.max_chars - len(key))
        if len(value) <= max_value_chars:
            return value
        logger.info(
            "[CoreMemory] value for key=%s trimmed to fit max_chars=%s",
            key,
            self.max_chars,
        )
        return value[:max_value_chars]

    def _active_usage_excluding(self, conn, key: str) -> tuple[int, int]:
        rows = conn.execute(
            "SELECT key, value FROM core_memory WHERE status = 'active' AND key != ?",
            (key,),
        ).fetchall()
        chars = sum(len(str(row["key"])) + len(str(row["value"])) for row in rows)
        return len(rows), chars

    def _evict_until_room(self, conn, *, key: str, new_entry_chars: int, replacing: bool) -> None:
        while True:
            active_count, current_chars = self._active_usage_excluding(conn, key)
            projected_count = active_count + (0 if replacing else 1)
            projected_chars = current_chars + new_entry_chars
            if projected_count <= self.max_entries and projected_chars <= self.max_chars:
                return

            victim = conn.execute(
                "SELECT key FROM core_memory "
                "WHERE status = 'active' AND key != ? AND pinned = 0 "
                "ORDER BY CASE WHEN source = 'ai' THEN 0 ELSE 1 END, updated_at ASC "
                "LIMIT 1",
                (key,),
            ).fetchone()
            if victim is None:
                logger.warning(
                    "[CoreMemory] no evictable memory found; allowing overflow "
                    "(count=%s/%s chars=%s/%s)",
                    projected_count,
                    self.max_entries,
                    projected_chars,
                    self.max_chars,
                )
                return
            now = time.time()
            conn.execute(
                "UPDATE core_memory SET status = 'suppressed', deleted_at = ?, updated_at = ? WHERE key = ?",
                (now, now, victim["key"]),
            )
            logger.info("[CoreMemory] evicted key=%s to make room", victim["key"])
