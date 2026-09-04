from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OpenAIMessage(BaseModel):
    # Allow to have extra openAI attributes, like  `tool_calls`,
    # `function_call`, etc. Pydantic's default `extra="ignore"`
    # drops them.
    #
    # `extra="allow"` renders as `additionalProperties: true`, which makes
    # Swagger UI invent an `"additionalProp1": {}` key in the body it generates
    # for "Try it out". Extra keys are forwarded verbatim to the downstream
    # OpenAI-compatible provider, so that generated body 400s as soon as it is
    # copied into curl. Every model here therefore pins an explicit `examples`
    # entry, which Swagger UI renders in place of its own guess.
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={"examples": [{"role": "user", "content": "What is OpenRag?"}]},
    )

    role: Literal["user", "assistant", "system", "tool", "developer"]

    # content can be None when using `tool_calls`
    content: str | None = None


class OpenAIChatCompletionRequest(BaseModel):
    # Accept and forward vendor-specific OpenAI params. See `OpenAIMessage` for
    # why an example is pinned; here it also keeps Swagger UI from filling the
    # unset optionals with placeholders that are *valid* JSON but invalid
    # requests -- `max_tokens: 0` (rejected downstream, minimum is 1) and
    # `logprobs: true` / `top_logprobs: 0` (opt-in only). The example
    # omits every field that should stay omitted, so it is copy-pasteable.
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "examples": [
                {
                    "model": "openrag-<partition>",
                    "messages": [{"role": "user", "content": "What is OpenRag?"}],
                    "temperature": 0.3,
                    "top_p": 1.0,
                    "stream": False,
                    "metadata": {
                        "use_map_reduce": False,
                        "spoken_style_answer": False,
                        "websearch": False,
                        "include_all_retrieved_sources": False,
                    },
                }
            ]
        },
    )

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
    logprobs: bool | None = Field(None)
    top_logprobs: int | None = Field(None)
    response_format: dict[str, Any] | None = Field(
        None,
        examples=[{"type": "json_object"}],
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
        },
        description=(
            "Extra custom parameters. Supports an 'llm_override' object with an optional 'model' "
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
    # Mirrors OpenAIChatCompletionRequest, including the pinned example that
    # keeps Swagger UI from generating an unusable body.
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "examples": [
                {
                    "model": "openrag-<partition>",
                    "prompt": "What is OpenRag?",
                    "temperature": 0.3,
                    "top_p": 1.0,
                    "stream": False,
                    "metadata": {
                        "spoken_style_answer": False,
                        "include_all_retrieved_sources": False,
                    },
                }
            ]
        },
    )

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
        },
        description=(
            "Extra custom parameters. Supports an 'llm_override' object with an optional 'model' "
            "to override the downstream model name; its 'base_url' and 'api_key' are honored only "
            "when the deployment sets LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT, and ignored otherwise. "
            "'include_all_retrieved_sources' (default false) adds the full, unfiltered retrieval "
            "set to the response's extra.all_retrieved_sources — off by default since it can be "
            "large; opt in only for debugging/evaluation."
        ),
    )
