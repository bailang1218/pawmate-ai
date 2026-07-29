import json

import pytest

from pawmate.core.runtime.turn_context_builder import TurnContextBuilder
from pawmate.core.tools.tool_call_runner import ToolCallRunner
from pawmate.memory.long_term_memory_manager import LongTermMemoryManager
from pawmate.memory.memory_store import MemoryStore
from pawmate.memory.policy import MemoryKind, evaluate_memory_write
from pawmate.storage.session_store import SessionStore
from pawmate.tools.memory.tools import register_memory_tools
from pawmate.tools.core.registry import APPROVAL_CONFIRM, RiskLevel, SideEffectLevel, ToolCategory, ToolRegistry


def test_memory_write_policy_rejects_sensitive_and_transient_data():
    sensitive = evaluate_memory_write(
        kind=MemoryKind.LONG_TERM_USER_MEMORY,
        key="api_key",
        content="sk-secret12345",
        explicit_user_intent=True,
    )
    transient = evaluate_memory_write(
        kind=MemoryKind.LONG_TERM_USER_MEMORY,
        key="today_only",
        content="remember this just this once",
        explicit_user_intent=True,
    )
    implicit = evaluate_memory_write(
        kind=MemoryKind.LONG_TERM_USER_MEMORY,
        key="preference",
        content="user likes concise summaries",
        explicit_user_intent=False,
    )

    assert sensitive.allowed is False
    assert sensitive.reason == "sensitive_memory_requires_user_review"
    assert transient.allowed is False
    assert transient.reason == "transient_or_one_time_fact"
    assert implicit.allowed is False
    assert implicit.reason == "long_term_memory_requires_explicit_user_intent"


def test_memory_manager_filters_and_traces_injected_memory(tmp_path):
    manager = LongTermMemoryManager(MemoryStore(tmp_path / "memory.db"))
    manager.core.create_manual("nickname", "user likes concise updates")
    manager.core.create_manual("api_key", "sk-secret12345")

    manager.episodic.search = lambda _query, k=3: [
        {
            "rowid": 1,
            "title": "good",
            "content": "User prefers concise progress updates.",
            "created_at": 1.0,
            "retrieval": "vector",
            "hybrid_score": 0.82,
        },
        {
            "rowid": 2,
            "title": "low",
            "content": "Unrelated low-score memory.",
            "created_at": 1.0,
            "retrieval": "vector",
            "hybrid_score": 0.01,
        },
        {
            "rowid": 3,
            "title": "secret",
            "content": "token=sk-secret12345",
            "created_at": 1.0,
            "retrieval": "vector",
            "hybrid_score": 0.99,
        },
    ]

    block = manager.build_memory_block("How should you update me?")
    trace = manager.get_last_injection_trace()

    assert "user likes concise updates" in block
    assert "api_key" not in block
    assert "User prefers concise progress updates." in block
    assert "Unrelated low-score memory" not in block
    assert "token=" not in block
    assert "score=0.820" in block
    assert "reason=relevant_to_current_user_message" in block
    assert any(item["included"] is False and item["reason"] == "below_relevance_threshold" for item in trace)
    assert any(item["included"] is False and item["sensitive"] is True for item in trace)


def test_turn_context_trace_includes_memory_injection_metadata():
    class FakeConversation:
        def build_context_block(self):
            return ""

    class FakeMemory:
        def build_memory_block(self, _user_message):
            return "memory"

        def get_last_injection_trace(self):
            return [{"kind": "EPISODIC_MEMORY", "included": True, "score": 0.8}]

    builder = TurnContextBuilder(
        static_prompt_provider=lambda: "rules",
        conversation_context_provider=FakeConversation,
        long_term_memory_provider=FakeMemory,
        runtime_context_provider=lambda: "",
    )

    builder.compose_runtime_prompt("hello", turn_id="m1")
    sections = {item["name"]: item for item in builder.get_last_trace()["sections"]}

    metadata = sections["DATA_ONLY_MEMORY_CONTEXT"]["metadata"]
    assert metadata["injection_trace"][0]["kind"] == "EPISODIC_MEMORY"
    assert metadata["injection_trace"][0]["score"] == 0.8


@pytest.mark.asyncio
async def test_memory_tools_apply_write_policy_and_metadata(tmp_path):
    manager = LongTermMemoryManager(MemoryStore(tmp_path / "memory.db"))
    registry = ToolRegistry()
    register_memory_tools(registry, manager)

    result = await registry.execute("core_remember", {"key": "api_key", "value": "sk-secret12345"})
    core_forget = registry.get_tool("core_forget")
    search_memory = registry.get_tool("search_memory")

    assert result.ok is False
    payload = json.loads(result.to_legacy_string())
    assert payload["error_type"] == "memory_write_rejected"
    assert payload["reason"] == "sensitive_memory_requires_user_review"
    assert core_forget.approval == APPROVAL_CONFIRM
    assert core_forget.category == ToolCategory.MEMORY.value
    assert core_forget.risk == RiskLevel.HIGH.value
    assert core_forget.side_effect == SideEffectLevel.DESTRUCTIVE.value
    assert search_memory.side_effect == SideEffectLevel.READ_ONLY.value


@pytest.mark.asyncio
async def test_explicit_chinese_memory_tool_returns_verified_success(tmp_path):
    db_path = tmp_path / "memory.db"
    manager = LongTermMemoryManager(MemoryStore(db_path))
    sessions = SessionStore(db_path)
    sessions.ensure_session("s1")
    user_seq = sessions.append_message("s1", "user", "你不存一下记忆我在哪？")
    manager.set_current_conversation("s1", user_seq, user_seq)
    registry = ToolRegistry()
    register_memory_tools(registry, manager)
    registry.set_confirm_callback(lambda _name, _input: True)

    outcome = await ToolCallRunner(registry).run(
        "remember_memory",
        {
            "item_type": "profile",
            "content": "用户住在浙江省建德市",
            "predicate": "城市",
            "explicit_user_intent": True,
            "user_request_excerpt": "你不存一下记忆我在哪？",
        },
    )

    assert outcome.success is True
    payload = json.loads(outcome.views.model)
    assert payload["ok"] is True
    assert payload["verified"] is True
    assert payload["memory"]["predicate"] == "location.city"
    assert manager.search_memories("我在哪", limit=1)[0]["content"] == "用户住在浙江省建德市"


@pytest.mark.asyncio
async def test_consider_memory_tool_can_activate_direct_durable_user_fact(tmp_path):
    db_path = tmp_path / "memory.db"
    manager = LongTermMemoryManager(MemoryStore(db_path))
    sessions = SessionStore(db_path)
    sessions.ensure_session("s1")
    user_seq = sessions.append_message("s1", "user", "I live in Jiande, Zhejiang.")
    manager.set_current_conversation("s1", user_seq, user_seq)
    registry = ToolRegistry()
    register_memory_tools(registry, manager)

    outcome = await ToolCallRunner(registry).run(
        "consider_memory",
        {
            "item_type": "profile",
            "content": "The user lives in Jiande, Zhejiang.",
            "predicate": "location.city",
            "user_evidence_excerpt": "I live in Jiande, Zhejiang.",
            "durability": "long_term",
            "future_utility": "high",
            "confidence": 0.96,
            "rationale": "Useful for future local and weather requests.",
        },
    )

    assert outcome.success is True
    payload = json.loads(outcome.views.model)
    assert payload["ok"] is True
    assert payload["activated"] is True
    assert payload["verified"] is True
    assert payload["memory"]["status"] == "active"
