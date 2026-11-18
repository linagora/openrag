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
    TOPIC_TAGGER = "topic_tagger"
    ASR_TRANSCRIPTION = "asr_transcription"


#: Types of features that were removed: the ``hyde`` and ``multiQuery``
#: retrievers. Their stored prompts stay valid so an admin can read and delete
#: them, but they are no longer seeded and no new one can be created.
RETIRED_PROMPT_TYPES = frozenset({PromptType.HYDE.value, PromptType.MULTI_QUERY.value})


class Prompt(BaseModel):
    """A prompt template stored in the library."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    prompt_type: str = ""
    name: str = ""
    content: str = ""
    is_default: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
