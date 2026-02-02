from typing import Any, Literal

from pydantic import BaseModel, Field


# Classes pour la compatibilité OpenAI
class OpenAIMessage(BaseModel):
    """Modèle représentant un message dans l'API OpenAI."""

    role: Literal["user", "assistant", "system"]
    content: str


class OpenAIChatCompletionRequest(BaseModel):
    """Modèle représentant une requête de complétion chat pour l'API OpenAI."""

    model: str | None = Field(None, description="model name")
    messages: list[OpenAIMessage]
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
    stream: bool | None = Field(False)
    max_tokens: int | None = Field(1024)
    logprobs: int | None = Field(None)
    metadata: dict[str, Any] | None = Field(
        {
            "use_map_reduce": False,
            "spoken_style_answer": False,
        },
        description="Extra custom parameters. Supports 'llm_override' object with optional 'base_url', 'api_key', and 'model' to override the downstream LLM endpoint.",
    )


class OpenAICompletionRequest(BaseModel):
    """Legacy OpenAI completion API"""

    model: str | None = Field(None, description="model name")
    prompt: str
    best_of: int | None = Field(1)
    echo: bool | None = Field(False)
    frequency_penalty: float | None = Field(0.0)
    logit_bias: dict | None = Field(None)
    logprobs: int | None = Field(None)
    max_tokens: int | None = Field(512)
    n: int | None = Field(1)
    presence_penalty: float | None = Field(0.0)
    seed: int | None = Field(None)
    stop: list[str] | None = Field(None)
    stream: bool | None = Field(False)
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
