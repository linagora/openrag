from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def default_max_tokens():
    from core.config import load_config

    return load_config().llm_context.max_output_tokens


class OpenAIMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class OpenAIChatCompletionRequest(BaseModel):
    # Accept and forward vendor-specific OpenAI params
    model_config = ConfigDict(extra="allow")

    model: str | None = Field(None, description="model name")
    messages: list[OpenAIMessage]
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
    stream: bool | None = Field(False)
    max_tokens: int | None = Field(default_factory=default_max_tokens)
    logprobs: int | None = Field(None)
    response_format: dict[str, Any] | None = Field(
        None,
        description="OpenAI response_format, e.g. {'type': 'json_object'} or "
        "{'type': 'json_schema', 'json_schema': {...}}. Forwarded to the LLM. "
        "Note: forcing JSON output on a partition (RAG) query suppresses the "
        "inline [Sources: N] citations, so all retrieved sources are returned.",
    )
    metadata: dict[str, Any] | None = Field(
        {
            "use_map_reduce": False,
            "spoken_style_answer": False,
            "websearch": False,
            "llm_override": None,
        },
        description="Extra custom parameters. Supports 'llm_override' object with an optional 'model' to override the downstream model name. The LLM endpoint and credentials are fixed by server configuration and cannot be overridden by the client.",
    )


class OpenAICompletionRequest(BaseModel):
    model: str | None = Field(None, description="model name")
    prompt: str
    # Bound n/best_of: each multiplies generation cost, so leaving them unbounded
    # lets one request fan out into a resource-exhaustion amplifier.
    best_of: int | None = Field(1, ge=1, le=8)
    echo: bool | None = Field(False)
    frequency_penalty: float | None = Field(0.0)
    logit_bias: dict | None = Field(None)
    logprobs: int | None = Field(None)
    max_tokens: int | None = Field(default_factory=default_max_tokens)
    n: int | None = Field(1, ge=1, le=8)
    presence_penalty: float | None = Field(0.0)
    seed: int | None = Field(None)
    stop: list[str] | None = Field(None)
    stream: bool | None = Field(False)
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
