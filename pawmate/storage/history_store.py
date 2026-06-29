"""
HistoryStore — drop-in compatibility layer over SessionStore.

Interface identical to the old in-memory version: engine.py sees no difference.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pawmate.storage.session_store import SessionStore


class HistoryStore:
    """Drop-in replacement: interface unchanged, backed by SQLite."""

    def __init__(self, session_id: str = "default"):
        self._store = SessionStore()
        self._sid = session_id
        self._store.ensure_session(self._sid)

    # ── public API (unchanged signatures) ──────────────────────

    @property
    def session_id(self) -> str:
        """Current conversation/session id for this history container."""
        return self._sid

    @property
    def session_store(self) -> SessionStore:
        """Expose the backing session store through a named seam."""
        return self._store

    def replace_session(self, session_id: str) -> None:
        """Point this history container at another session without replacing it."""
        self._sid = session_id
        self._store.ensure_session(self._sid)

    def clear(self) -> None:
        """Delete all messages for this session, then re-create it."""
        self._store.delete_session(self._sid)
        self._store.ensure_session(self._sid)

    def get_messages(self) -> List[Dict[str, Any]]:
        """Return all messages, preserving the original dict format."""
        rows = self._store.get_messages(self._sid)
        return self._clean_orphan_tool_messages(self._rows_to_dicts(rows))

    @staticmethod
    def _rows_to_dicts(rows):
        result: List[Dict[str, Any]] = []
        for msg in rows:
            content = msg["content"]
            role = msg["role"]

            if role == "tool" and isinstance(content, dict):
                entry: Dict[str, Any] = {
                    "role": "tool",
                    "content": content.get("result", ""),
                    "tool_use_id": content.get("tool_use_id", ""),
                    "tool_name": content.get("tool_name", "tool"),
                }
            else:
                entry = {"role": role, "content": content}
            if "seq" in msg:
                entry["seq"] = msg.get("seq")
            if "created_at" in msg:
                entry["created_at"] = msg.get("created_at")
            result.append(entry)
        return result

    def append_message(self, role: str, content: Any, **extra: Any) -> int:
        """Add a raw message. **extra is ignored for backward compat."""
        return self._store.append_message(self._sid, role, content)

    def append_user(self, content: Any) -> int:
        """Add a user message."""
        return self._store.append_message(self._sid, "user", content)

    def append_assistant(self, content: Any) -> int:
        """Add an assistant message (str or dict with tool_calls)."""
        return self._store.append_message(self._sid, "assistant", content)

    def append_tool_result(
        self, tool_use_id: str, result: Any, tool_name: str = "tool"
    ) -> int:
        """Add a tool result, packed into a structured content dict."""
        return self._store.append_message(self._sid, "tool", {
            "result": result,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
        })

    def get_messages_range(
        self,
        start_seq: int,
        end_seq: int,
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        """Return messages in a seq range, preserving seq for compaction."""
        rows = self._store.get_messages_range(self._sid, start_seq, end_seq, limit=limit)
        result: List[Dict[str, Any]] = []
        for msg in rows:
            entry = self._rows_to_dicts([msg])[0]
            entry["seq"] = msg.get("seq")
            entry["created_at"] = msg.get("created_at")
            result.append(entry)
        return result

    def get_messages_windowed(
        self,
        max_chars: int = 48000,
        keep_first_n: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        返回经过上下文窗口限制的消息列表。
        策略：保留开头 keep_first_n 条 + 最近若干条，中间插截断标记。
        """
        all_messages = self.get_messages()
        total = sum(self._estimate_chars(m) for m in all_messages)
        if total <= max_chars or len(all_messages) <= keep_first_n + 1:
            return self._for_model_messages(self._clean_orphan_tool_messages(all_messages))

        head = all_messages[:keep_first_n]
        head_chars = sum(self._estimate_chars(m) for m in head)
        remaining = max_chars - head_chars - 100

        tail = []
        candidates = all_messages[keep_first_n:]
        for msg in reversed(candidates):
            mc = self._estimate_chars(msg)
            if remaining - mc < 0:
                break
            tail.insert(0, msg)
            remaining -= mc

        if len(tail) >= len(candidates):
            return self._for_model_messages(self._clean_orphan_tool_messages(all_messages))

        truncated = len(candidates) - len(tail)
        marker = {
            "role": "user",
            "content": f"[...earlier {truncated} messages omitted for context window limit...]",
        }
        result = head + [marker] + tail
        return self._for_model_messages(self._clean_orphan_tool_messages(result))

    @staticmethod
    def _estimate_chars(msg: Dict[str, Any]) -> int:
        import json as _json
        content = msg.get("content", "")
        if isinstance(content, str):
            return len(content)
        if isinstance(content, (dict, list)):
            return len(_json.dumps(content, ensure_ascii=False))
        return len(str(content))

    @staticmethod
    def _clean_orphan_tool_messages(messages):
        """
        双向清理工具调用孤儿消息，严格遵循 OpenAI/DeepSeek 协议约束。

        1. 移除前面没有 tool_calls 的孤儿 tool 消息
        2. 移除后面没有对应 tool_result 的孤儿 assistant(tool_calls) 消息

        第 2 点是关键：当 get_messages_windowed 因上下文限制截断历史时，
        可能砍掉 tool_result 却保留了前面带 tool_calls 的 assistant 消息，
        导致 DeepSeek API 返回 400:
        "An assistant message with 'tool_calls' must be followed by
         tool messages responding to each 'tool_call_id'."
        """
        # Pass 1: 移除孤儿 tool 消息
        # 必须检查 tool_use_id 是否匹配前面 assistant 的 tool_call id，
        # 只检查"前面有没有 assistant"不够——截断后可能 A 的 tool_calls 后面跟的是
        # B 的 tool_result，ID 对不上。
        cleaned = []
        for msg in messages:
            if msg.get("role") == "tool":
                prev_assistant = None
                prev_idx = -1
                for idx, m in enumerate(reversed(cleaned)):
                    if m.get("role") == "assistant":
                        prev_assistant = m
                        prev_idx = len(cleaned) - 1 - idx
                        break
                has_match = False
                if prev_assistant is not None:
                    content = prev_assistant.get("content")
                    if isinstance(content, dict):
                        tc_arr = content.get("tool_calls") or []
                        tids = {tc.get("id") for tc in tc_arr if tc.get("id")}
                        if msg.get("tool_use_id", "") in tids:
                            has_match = True
                if not has_match:
                    continue  # 跳过孤儿 tool 消息
            cleaned.append(msg)

        # Pass 2: 移除孤儿 assistant(tool_calls) 消息
        # （它声称调用了工具但后面没有对应的 tool_result）
        result = []
        i = 0
        while i < len(cleaned):
            msg = cleaned[i]
            needs_check = (
                msg.get("role") == "assistant"
                and isinstance(msg.get("content"), dict)
                and bool(msg["content"].get("tool_calls"))
            )
            if needs_check:
                tool_call_ids = {
                    tc.get("id") for tc in msg["content"]["tool_calls"]
                    if tc.get("id")
                }
                if tool_call_ids:
                    # 往后找，直到下一个 assistant 消息，收集匹配的 tool_result
                    found_ids = set()
                    j = i + 1
                    while j < len(cleaned) and cleaned[j].get("role") != "assistant":
                        if cleaned[j].get("role") == "tool":
                            tid = cleaned[j].get("tool_use_id", "")
                            if tid in tool_call_ids:
                                found_ids.add(tid)
                        j += 1

                    # 有任何一个 tool_call_id 缺了对应的 tool_result → 整个消息是孤儿
                    if found_ids != tool_call_ids:
                        i += 1
                        continue

            result.append(msg)
            i += 1

        return result

    @staticmethod
    def _for_model_messages(messages):
        """Strip storage/UI metadata before handing history to provider formatters."""
        result = []
        for msg in messages:
            role = msg.get("role")
            if role == "tool":
                entry = {
                    "role": "tool",
                    "content": msg.get("content", ""),
                    "tool_use_id": msg.get("tool_use_id", ""),
                    "tool_name": msg.get("tool_name", "tool"),
                }
            else:
                entry = {
                    "role": role,
                    "content": msg.get("content", ""),
                }
            result.append(entry)
        return result

    # ── convenience ————————————————————————————————————————————

    def snapshot(self) -> int:
        """记录当前历史的最大 seq，用于后续 rollback。"""
        return self._store.get_max_seq(self._sid)

    def rollback_to(self, seq: int) -> int:
        """回滚到指定 seq 之后，删除所有更新的消息。"""
        return self._store.truncate_after_seq(self._sid, seq)

    def close(self) -> None:
        """Close underlying SQLite connections."""
        self._store.close()
