"""Conversation and message domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Message(BaseModel):
    """A single message within a conversation."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str = ""
    role: str = "user"
    content: str = ""
    sources_json: list[dict[str, Any]] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Conversation(BaseModel):
    """A persistent conversation between a user and the RAG system."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: int = 0
    partition_scope: str = "default"
    title: str = ""
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
