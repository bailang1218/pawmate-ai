"""Factory helpers for wiring conversation persistence implementations."""
from __future__ import annotations

from typing import Any

from pawmate.app.ports.conversation_repository import ConversationRepository
from pawmate.storage.conversation_repository import SessionConversationRepository


def create_conversation_repository_for_memory(ltm: Any) -> ConversationRepository:
    """Create the conversation repository that shares the LTM database path."""
    return SessionConversationRepository(db_path=ltm.store.db_path)

