"""
Backward-compatible facade for the long-term memory subsystem.

New engine code uses LongTermMemoryManager directly. This class remains for
older imports and exposes the historical build_prompt_block() method name.
"""
from __future__ import annotations

from dataclasses import dataclass

from .long_term_memory_manager import LongTermMemoryManager
from .memory_store import MemoryStore


@dataclass(slots=True)
class MemoryStatus:
    enabled: bool
    storage: str
    core_count: int
    core_capacity: int
    episodic_count: int


class MemoryManager(LongTermMemoryManager):
    def __init__(
        self,
        store: MemoryStore,
        *,
        user_id: str = "local-default",
        companion_id: str = "default-pet",
        conversation_id: str = "default-session",
    ):
        super().__init__(
            store,
            user_id=user_id,
            companion_id=companion_id,
        )
        self.conversation_id = conversation_id
        self.set_current_conversation(conversation_id)
        self.consolidator = None

    def build_prompt_block(self, user_message: str = "") -> str:
        return self.build_memory_block(user_message)

    async def maybe_consolidate(self) -> None:
        return None
