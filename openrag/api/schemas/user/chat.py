from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OpenAIMessage(BaseModel):
    # Allow to have extra openAI attributes, like  `tool_calls`,
    # `function_call`, etc. Pydantic's default `extra="ignore"`
    # drops them.
    model_config = ConfigDict(extra="allow")

    role: Literal["user", "assistant", "system", "tool", "developer"]

    # content can be None when using `tool_calls`
    content: str | None = None


class OpenAIChatCompletionRequest(BaseModel):
    # Accept and forward vendor-specific OpenAI params
    model_config = ConfigDict(extra="allow")

    model: str | None = Field(None, description="model name")
    messages: list[OpenAIMessage]
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
    stream: bool | None = Field(False)
    # Deliberately left unset rather than defaulted here: this schema is parsed
    # before the request's partition (and therefore the answering LLM endpoint)
    # is known, so a default_factory could only ever read the *default*
    # endpoint's budget — capping a partition whose own chat_llm endpoint allows
    # more. The router fills it from the resolved endpoint via
    # ``_apply_default_max_tokens`` once the partition is known.
    max_tokens: int | None = Field(None)
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
            "include_all_retrieved_sources": False,
            "require_retrieval": False,
        },
        description=(
            "Extra custom parameters. 'require_retrieval' (default false; enabled only by JSON true) "
            "forces retrieval even for a casual or normalized-empty partition-backed chat message. "
            "Other partition-backed chat messages retrieve by default. Common casual messages are recognized "
            "directly; the contextualizer may conservatively recognize additional messages whose complete intent "
            "is only a greeting, gratitude, farewell, or a capability question. Ambiguous, mixed, and factual "
            "messages retrieve, and an empty generated query falls back to the latest user message. Casual messages "
            "that are not forced use a direct response. Search scope and "
            "filters remain in effect; matching sources are not guaranteed. Has no effect in direct LLM mode. "
            "Supports an 'llm_override' object with an optional 'model' "
            "to override the downstream model name; its 'base_url' and 'api_key' are honored only "
            "when the deployment sets LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT, and ignored otherwise. "
            "'include_all_retrieved_sources' (default false) adds the full, unfiltered retrieval "
            "set to the response's extra.all_retrieved_sources — off by default since it can be "
            "large; opt in only for debugging/evaluation."
        ),
    )

    @model_validator(mode="after")
    def _ignore_top_logprobs_without_logprobs(self) -> "OpenAIChatCompletionRequest":
        # OpenAI semantics: `top_logprobs` only applies when `logprobs` is
        # enabled. Mirror that by silently dropping it otherwise (rather than
        # raising) — stays OpenAI-compatible while never forwarding an invalid
        # pair to strict downstream providers that reject `top_logprobs` unless
        # `logprobs` is true. Dropped to None so model_dump(exclude_none=True)
        # omits it entirely.
        if self.top_logprobs is not None and not self.logprobs:
            self.top_logprobs = None
        return self


class OpenAICompletionRequest(BaseModel):
    # Mirrors OpenAIChatCompletionRequest
    model_config = ConfigDict(extra="allow")

    model: str | None = Field(None, description="model name")
    prompt: str
    # Bound n/best_of: each multiplies generation cost, so leaving them unbounded
    # lets one request fan out into a resource-exhaustion amplifier.
    best_of: int | None = Field(1, ge=1, le=8)
    echo: bool | None = Field(False)
    frequency_penalty: float | None = Field(0.0)
    logit_bias: dict | None = Field(None)
    logprobs: int | None = Field(None)
    # Deliberately left unset rather than defaulted here: this schema is parsed
    # before the request's partition (and therefore the answering LLM endpoint)
    # is known, so a default_factory could only ever read the *default*
    # endpoint's budget — capping a partition whose own chat_llm endpoint allows
    # more. The router fills it from the resolved endpoint via
    # ``_apply_default_max_tokens`` once the partition is known.
    max_tokens: int | None = Field(None)
    n: int | None = Field(1, ge=1, le=8)
    presence_penalty: float | None = Field(0.0)
    seed: int | None = Field(None)
    stop: list[str] | None = Field(None)
    stream: bool | None = Field(False)
    temperature: float | None = Field(0.3)
    top_p: float | None = Field(1.0)
    metadata: dict[str, Any] | None = Field(
        {
            "spoken_style_answer": False,
            "llm_override": None,
            "include_all_retrieved_sources": False,
            "require_retrieval": False,
        },
        description=(
            "Extra custom parameters. 'require_retrieval' (default false; enabled only by JSON true) "
            "makes partition-backed text completions retrieve when the contextualizer would otherwise skip, "
            "using the original prompt as the fallback query. Unlike chat, text completions remain opt-in when "
            "the contextualizer skips retrieval. Search scope and filters remain in effect; matching sources "
            "are not guaranteed. Has no effect in direct LLM mode. "
            "Supports an 'llm_override' object with an optional 'model' "
            "to override the downstream model name; its 'base_url' and 'api_key' are honored only "
            "when the deployment sets LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT, and ignored otherwise. "
            "'include_all_retrieved_sources' (default false) adds the full, unfiltered retrieval "
            "set to the response's extra.all_retrieved_sources — off by default since it can be "
            "large; opt in only for debugging/evaluation."
        ),
    )
