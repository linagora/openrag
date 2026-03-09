from typing import Any, Literal

from config import load_config
from pydantic import BaseModel, Field

config = load_config()
default_max_tokens = int(config.llm_context.get("max_output_tokens", 1024))


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
    max_tokens: int | None = Field(default_max_tokens)
    logprobs: int | None = Field(None)
    metadata: dict[str, Any] | None = Field(
        {
            "use_map_reduce": False,
            "spoken_style_answer": False,
            "websearch": False,
            "llm_override": None,
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
    max_tokens: int | None = Field(default_max_tokens)
    n: int | None = Field(1)
    presence_penalty: float | None = Field(0.0)
    seed: int | None = Field(None)
    stop: list[str] | None = Field(None)
    stream: bool | None = Field(False)
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
