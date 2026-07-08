"""Stub :class:`ConversationRepository`.

OpenRAG does not persist chat history today — Chainlit handles transient
session state and nothing is written to Postgres. The post-refactoring
P2 feature is DB-persisted conversations + messages, used both for
resume-on-reconnect UX and as a source for fine-tuning datasets.
"""

from __future__ import annotations

from core.models.conversation import Conversation, Message
from core.ports.conversation_repo import ConversationRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgConversationRepository(_StubRepositoryBase, ConversationRepository):
    """TODO: real impl once chat persistence ships."""

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        raise stub_not_implemented("Chat persistence")

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        raise stub_not_implemented("Chat persistence")

    async def list_conversations(
        self,
        user_id: int,
        partition: str | None = None,
    ) -> list[Conversation]:
        raise stub_not_implemented("Chat persistence")

    async def delete_conversation(self, conversation_id: str) -> bool:
        raise stub_not_implemented("Chat persistence")

    async def add_message(self, message: Message) -> Message:
        raise stub_not_implemented("Chat persistence")

    async def list_messages(self, conversation_id: str) -> list[Message]:
        raise stub_not_implemented("Chat persistence")


__all__ = ["PgConversationRepository"]
