"""Prompt domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class PromptType(str, Enum):
    SYS_PROMPT = "sys_prompt"
    QUERY_CONTEXTUALIZER = "query_contextualizer"
    CHUNK_CONTEXTUALIZER = "chunk_contextualizer"
    IMAGE_CAPTIONING = "image_captioning"
    HYDE = "hyde"
    MULTI_QUERY = "multi_query"
    SPOKEN_STYLE_ANSWER = "spoken_style_answer"


class Prompt(BaseModel):
    """A prompt template stored in the library."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    prompt_type: str = ""
    name: str = ""
    content: str = ""
    is_default: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
