from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# Classes pour la compatibilité OpenAI
class OpenAIMessage(BaseModel):
    """Modèle représentant un message dans l'API OpenAI."""

    role: Literal["user", "assistant", "system"]
    content: str


class OpenAIChatCompletionRequest(BaseModel):
    """Modèle représentant une requête de complétion chat pour l'API OpenAI."""

    model: str = Field(..., description="model name")
    messages: List[OpenAIMessage]
    temperature: Optional[float] = Field(0.3)
    top_p: Optional[float] = Field(1.0)
    stream: Optional[bool] = Field(False)
    max_tokens: Optional[int] = Field(1024)
    logprobs: Optional[int] = Field(None)
    metadata: Optional[Dict[str, Any]] = Field(
        {
            "use_map_reduce": False,
        },
        description="Extra parameters for OpenAI API",
    )


class OpenAIChatCompletionChoice(BaseModel):
    """Modèle représentant un choix de complétion chat dans l'API OpenAI."""

    index: int
    message: OpenAIMessage
    finish_reason: str


class OpenAILogprobs(BaseModel):
    text_offset: Optional[List] = Field(None)
    token_logprobs: Optional[List[float]] = Field(None)
    tokens: Optional[List[str]] = Field(None)
    top_logprobs: Optional[List] = Field(None)


class ChatCompletionTokenLogprob(BaseModel):
    token: str
    bytes_: list[int] = Field(..., alias="bytes")  # Handle Python reserved word
    logprob: float
    top_logprobs: list  # Separate model for recursion


class ChoiceLogprobs(BaseModel):
    text_offsets: Optional[List[int]] = Field(None)
    token_logprobs: List[Union[float, None]]
    tokens: List[str] = Field(None)
    top_logprobs: List[Union[dict, None]]


# Handle forward references
ChatCompletionTokenLogprob.model_rebuild()


class OpenAICompletionChoice(BaseModel):
    """Modèle représentant un choix de complétion dans l'API OpenAI."""

    index: int
    text: str
    logprobs: Optional[ChoiceLogprobs] = Field(None)
    finish_reason: str


class OpenAICompletionRequest(BaseModel):
    """Legacy OpenAI completion API"""

    model: str = Field(..., description="model name")
    prompt: str
    best_of: Optional[int] = Field(1)
    echo: Optional[bool] = Field(False)
    frequency_penalty: Optional[float] = Field(0.0)
    logit_bias: Optional[dict] = Field(None)
    logprobs: Optional[int] = Field(None)
    max_tokens: Optional[int] = Field(512)
    n: Optional[int] = Field(1)
    presence_penalty: Optional[float] = Field(0.0)
    seed: Optional[int] = Field(None)
    stop: Optional[List[str]] = Field(None)
    stream: Optional[bool] = Field(False)
    temperature: Optional[float] = Field(0.3)
    top_p: Optional[float] = Field(1.0)


class OpenAIUsage(BaseModel):
    """Modèle représentant les statistiques d'utilisation dans l'API OpenAI."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class OpenAICompletion(BaseModel):
    """Modèle représentant une réponse de complétion dans l'API OpenAI."""

    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: List[OpenAICompletionChoice]
    usage: OpenAIUsage


class OpenAIChatCompletion(BaseModel):
    """Modèle représentant une réponse de complétion chat dans l'API OpenAI."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChatCompletionChoice]
    usage: OpenAIUsage


class OpenAICompletionChunkChoice(BaseModel):
    """Modèle représentant un choix de segment de complétion en streaming dans l'API OpenAI."""

    index: int
    delta: Dict[str, str]
    finish_reason: Optional[str] = None


class OpenAICompletionChunk(BaseModel):
    """Modèle représentant un segment de complétion en streaming dans l'API OpenAI."""

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[OpenAICompletionChunkChoice]
