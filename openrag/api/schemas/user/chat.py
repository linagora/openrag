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
    # Client-controlled and forwarded as-is to the downstream model; the server
    # default is off (see LLMParamsConfig.logprobs). For chat completions
    # `logprobs` is a boolean — `top_logprobs` carries the count — unlike the
    # legacy /completions endpoint where `logprobs` is an integer.
    logprobs: bool | None = Field(None)
    top_logprobs: int | None = Field(None)
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
        description="Extra custom parameters. Supports 'llm_override' object with optional 'base_url', 'api_key', and 'model' to override the downstream LLM endpoint.",
    )


class OpenAICompletionRequest(BaseModel):
    model: str | None = Field(None, description="model name")
    prompt: str
    best_of: int | None = Field(1)
    echo: bool | None = Field(False)
    frequency_penalty: float | None = Field(0.0)
    logit_bias: dict | None = Field(None)
    logprobs: int | None = Field(None)
    max_tokens: int | None = Field(default_factory=default_max_tokens)
    n: int | None = Field(1)
    presence_penalty: float | None = Field(0.0)
    seed: int | None = Field(None)
    stop: list[str] | None = Field(None)
    stream: bool | None = Field(False)
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
