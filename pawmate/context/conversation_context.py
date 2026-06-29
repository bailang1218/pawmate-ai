"""
ConversationContextManager — per-conversation context window management.

Keeps each conversation_id's context isolated:
- Recent turns (raw messages)
- Rolling summary (compacted old turns)
- Compacted cursor (where the fold stopped)

Phase 1: provides build_context_block() with KEEP_RECENT_TURNS.
Phase 4: will add maybe_consolidate() with FOLD_BATCH_SIZE.
"""
from __future__ import annotations

import logging
import json
from typing import Optional

_logger = logging.getLogger("pawmate")


class ConversationContextManager:
    KEEP_RECENT_TURNS = 24
    FOLD_BATCH_SIZE = 10
    MAX_FOLD_BATCHES_PER_PASS = 4

    def __init__(
        self,
        history_store,
        meta_store,
        conversation_id: str,
    ):
        self.history_store = history_store
        self.meta_store = meta_store
        self.conversation_id = conversation_id
        # Phase 4: llm reference for fold_context()
        self._llm = None

    # ── Phase 1: build context block ─────────────────────────

    def build_context_block(self) -> str:
        """
        Build the context block for the current conversation.
        Includes: rolling summary + recent messages.
        Each conversation_id is isolated.
        """
        summary = self.meta_store.get_meta(
            scope_type="conversation",
            scope_id=self.conversation_id,
            key="rolling_summary",
            default=None,
        )

        parts = ["<conversation_context>\n"]

        if summary:
            parts.append(f"对话摘要:\n{summary}")
            parts.append("")

        # Recent turns are loaded from history_store in engine.chat()
        # and passed via messages; here we just note the current window size.
        compacted = self.meta_store.get_meta(
            scope_type="conversation",
            scope_id=self.conversation_id,
            key="compacted_cursor",
            default=None,
        )

        if compacted:
            parts.append(f"已折叠到第 {compacted} 轮，摘要已包含之前的对话内容。")

        parts.append("</conversation_context>")
        return "\n".join(parts)

    # ── Phase 4: consolidation ───────────────────────────────

    def attach_llm(self, llm) -> None:
        """Phase 4: inject LLM for fold_context()."""
        self._llm = llm

    async def maybe_consolidate(self) -> None:
        """Fold older messages into a rolling summary without deleting history."""
        try:
            max_seq = int(self.history_store.snapshot())
        except Exception as exc:
            _logger.debug("[Context] snapshot unavailable: %s", exc)
            return

        cursor_raw = self.meta_store.get_meta(
            scope_type="conversation",
            scope_id=self.conversation_id,
            key="compacted_cursor",
            default="-1",
        )
        try:
            cursor = int(cursor_raw)
        except (TypeError, ValueError):
            cursor = -1

        folded = 0
        new_summary = ""
        while folded < self.MAX_FOLD_BATCHES_PER_PASS:
            fold_until = max_seq - self.KEEP_RECENT_TURNS
            if fold_until <= cursor:
                break

            available = fold_until - cursor
            if available < self.FOLD_BATCH_SIZE:
                break

            start_seq = cursor + 1
            end_seq = min(cursor + self.FOLD_BATCH_SIZE, fold_until)
            batch = self.history_store.get_messages_range(
                start_seq,
                end_seq,
                limit=self.FOLD_BATCH_SIZE + 8,
            )
            if not batch:
                break

            old_summary = self.meta_store.get_meta(
                scope_type="conversation",
                scope_id=self.conversation_id,
                key="rolling_summary",
                default="",
            ) or ""

            batch_text = self._render_messages_for_summary(batch)
            try:
                new_summary = await self._summarize(old_summary, batch_text)
            except Exception as exc:
                _logger.warning("[Context] LLM summary failed, using fallback: %s", exc)
                new_summary = self._fallback_summary(old_summary, batch_text)

            new_summary = new_summary.strip()[:8000]
            if not new_summary:
                break

            self.meta_store.set_meta(
                scope_type="conversation",
                scope_id=self.conversation_id,
                key="rolling_summary",
                value=new_summary,
            )
            self.meta_store.set_meta(
                scope_type="conversation",
                scope_id=self.conversation_id,
                key="compacted_cursor",
                value=str(end_seq),
            )
            cursor = end_seq
            folded += 1

        if folded == 0:
            return

        store = getattr(self.history_store, "_store", None)
        sid = getattr(self.history_store, "_sid", self.conversation_id)
        if store is not None and hasattr(store, "update_summary"):
            try:
                store.update_summary(sid, new_summary[:1000])
            except Exception as exc:
                _logger.debug("[Context] session summary update failed: %s", exc)

    @staticmethod
    def _render_messages_for_summary(messages) -> str:
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            seq = msg.get("seq", "?")
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            content = content.replace("\n", " ").strip()
            lines.append(f"[{seq}] {role}: {content}")
        return "\n".join(lines)[:12000]

    async def _summarize(self, old_summary: str, batch_text: str) -> str:
        if self._llm is None:
            return self._fallback_summary(old_summary, batch_text)

        prompt = (
            "Update the rolling summary for this conversation.\n"
            "Keep durable facts, decisions, user preferences, open tasks, and important context.\n"
            "Discard filler, repeated wording, and transient chatter.\n\n"
            f"Existing summary:\n{old_summary or '(none)'}\n\n"
            f"New messages:\n{batch_text}\n\n"
            "Return only the updated summary, concise but complete."
        )
        chunks: list[str] = []
        try:
            stream = self._llm.stream(
                messages=[{"role": "user", "content": prompt}],
                system="You maintain concise rolling conversation summaries.",
                tools=None,
            )
        except TypeError:
            stream = self._llm.stream(
                [{"role": "user", "content": prompt}],
                "You maintain concise rolling conversation summaries.",
            )

        async for event in stream:
            if isinstance(event, dict):
                if event.get("type") == "text_delta":
                    chunks.append(event.get("text", ""))
                elif event.get("type") == "message":
                    chunks.append(str(event.get("text", "")))
        text = "".join(chunks).strip()
        if text:
            return text
        response = getattr(self._llm, "get_last_response", lambda: None)()
        content = getattr(response, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return str(content.get("text", ""))
        return self._fallback_summary(old_summary, batch_text)

    @staticmethod
    def _fallback_summary(old_summary: str, batch_text: str) -> str:
        parts = []
        if old_summary:
            parts.append(old_summary.strip())
        if batch_text:
            parts.append("Recent folded messages:\n" + batch_text[:3000])
        return "\n\n".join(parts)

def render_conversation_context(summary: str, recent_messages) -> str:
    """Render conversation context for prompt injection."""
    parts = ["<conversation_context>"]
    if summary:
        parts.append(f"对话摘要:\n{summary}")
        parts.append("")
    parts.append("</conversation_context>")
    return "\n".join(parts)
