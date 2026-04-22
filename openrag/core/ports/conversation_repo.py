"""Conversation repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openrag.core.models.conversation import Conversation, Message


class ConversationRepository(ABC):
    """CRUD operations for conversations and messages."""

    @abstractmethod
    async def create_conversation(self, conversation: Conversation) -> Conversation: ...

    @abstractmethod
    async def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    @abstractmethod
    async def list_conversations(self, user_id: int, partition: str | None = None) -> list[Conversation]: ...

    @abstractmethod
    async def delete_conversation(self, conversation_id: str) -> bool: ...

    @abstractmethod
    async def add_message(self, message: Message) -> Message: ...

    @abstractmethod
    async def list_messages(self, conversation_id: str) -> list[Message]: ...
