import pytest
from components.llm import LLM
from config.models import LLMConfig
from models.openai import OpenAIChatCompletionRequest


@pytest.fixture
def llm():
    return LLM(
        LLMConfig(
            base_url="http://default-llm:8000/v1",
            api_key="default-key",
            model="default-model",
            temperature=0.3,
        )
    )


class TestExtractLlmOverrides:
    def test_no_override_uses_defaults(self, llm):
        """No llm_override: merge config defaults so the vLLM path keeps its
        tuned temperature/logprobs. Also re-fill max_tokens — the router's
        exclude_unset=True strips the Pydantic default from the dump, so we
        need a config-side fallback to keep generation bounded."""
        request = {
            "model": "openrag-my-partition",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        payload, base_url, headers = llm._extract_llm_overrides(request)

        assert payload["model"] == "default-model"
        assert payload["temperature"] == 0.3
        assert payload["max_tokens"] >= 1, "vLLM path must cap output tokens"
        assert payload["logprobs"] is True, "vLLM path keeps the configured logprobs request"
        assert base_url == "http://default-llm:8000/v1"
        assert headers["Authorization"] == "Bearer default-key"

    def test_override_all_fields(self, llm):
        request = {
            "model": "openrag-my-partition",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "metadata": {
                "llm_override": {
                    "base_url": "http://custom-llm:9000/v1",
                    "api_key": "custom-key",
                    "model": "custom-model",
                }
            },
        }
        payload, base_url, headers = llm._extract_llm_overrides(request)

        assert payload["model"] == "custom-model"
        assert base_url == "http://custom-llm:9000/v1"
        assert headers["Authorization"] == "Bearer custom-key"

    def test_trailing_slash_stripped_from_base_url(self, llm):
        request = {
            "model": "openrag-my-partition",
            "stream": False,
            "metadata": {"llm_override": {"base_url": "http://custom:8000/v1///", "model": "m"}},
        }
        _, base_url, _ = llm._extract_llm_overrides(request)

        assert base_url == "http://custom:8000/v1"

    def test_request_params_forwarded_to_payload(self, llm):
        request = {
            "model": "openrag-my-partition",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "max_tokens": 2048,
            "temperature": 0.9,
        }
        payload, _, _ = llm._extract_llm_overrides(request)

        assert payload["stream"] is True
        assert payload["max_tokens"] == 2048
        assert payload["temperature"] == 0.9
        assert payload["messages"] == [{"role": "user", "content": "hello"}]

    def test_metadata_without_llm_override_uses_defaults(self, llm):
        request = {
            "model": "openrag-my-partition",
            "stream": False,
            "metadata": {"use_map_reduce": True},
        }
        payload, base_url, headers = llm._extract_llm_overrides(request)

        assert payload["model"] == "default-model"
        assert base_url == "http://default-llm:8000/v1"
        assert headers["Authorization"] == "Bearer default-key"

    def test_llm_override_popped_from_metadata(self, llm):
        metadata = {
            "use_map_reduce": False,
            "llm_override": {"model": "custom"},
        }
        request = {"model": "x", "stream": False, "metadata": metadata}
        llm._extract_llm_overrides(request)

        assert "llm_override" not in metadata
        assert "use_map_reduce" in metadata

    def test_httpx_client_params_not_forwarded_on_override_path(self, llm):
        """`timeout` and `max_retries` are httpx client config, not LLM API
        params. OpenAI rejects them as 'Unrecognized request arguments'.

        The override path doesn't merge `default_llm_config`, so they can't
        leak into the payload regardless of how the config is shaped."""
        request = {
            "model": "openrag-p",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "metadata": {"llm_override": {"model": "gpt-4o"}},
        }
        payload, _, _ = llm._extract_llm_overrides(request)

        assert "timeout" not in payload
        assert "max_retries" not in payload

    def test_metadata_stripped_from_payload(self, llm):
        """OpenAI's chat completions metadata field requires Map<string,string>;
        openrag's internal pipeline flags (websearch, use_map_reduce, ...) would
        be rejected. The whole metadata dict must be stripped before forwarding."""
        request = {
            "model": "openrag-p",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "metadata": {
                "use_map_reduce": False,
                "websearch": True,
                "spoken_style_answer": False,
                "workspace": None,
            },
        }
        payload, _, _ = llm._extract_llm_overrides(request)

        assert "metadata" not in payload

    def test_thin_proxy_when_override_is_set(self, llm):
        """With llm_override set, config defaults (temperature, logprobs, ...)
        are NOT injected — let the upstream provider apply its own defaults so
        stricter schemas (Gemini, Mistral, o-series) aren't tripped."""
        request = {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "metadata": {"llm_override": {"model": "gpt-4o"}},
        }
        payload, _, _ = llm._extract_llm_overrides(request)

        # Fixture has temperature=0.3 on LLMConfig, but with llm_override active
        # we don't inject openrag's defaults.
        assert "temperature" not in payload
        assert "logprobs" not in payload
        assert "max_tokens" not in payload
        assert payload["model"] == "gpt-4o"

    def test_extra_fields_pass_through_with_override(self, llm):
        """Provider-specific fields (tools, response_format, reasoning_effort, ...)
        must flow through unchanged when llm_override targets a custom endpoint."""
        request = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "response_format": {"type": "json_object"},
            "seed": 42,
            "reasoning_effort": "high",
            "metadata": {"llm_override": {"model": "gpt-4o"}},
        }
        payload, _, _ = llm._extract_llm_overrides(request)

        assert payload["tools"] == [{"type": "function", "function": {"name": "f"}}]
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["seed"] == 42
        assert payload["reasoning_effort"] == "high"


class TestRequestModelPassthrough:
    """Verify the FastAPI request models accept and preserve unknown fields so
    clients can target any OpenAI-compatible provider."""

    def test_chat_request_accepts_extra_fields(self):
        req = OpenAIChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f"}}],
                "response_format": {"type": "json_object"},
                "reasoning_effort": "high",
            }
        )
        dump = req.model_dump(exclude_unset=True)
        assert dump["tools"] == [{"type": "function", "function": {"name": "f"}}]
        assert dump["response_format"] == {"type": "json_object"}
        assert dump["reasoning_effort"] == "high"

    def test_chat_request_exclude_unset_strips_pydantic_defaults(self):
        """When the client omits a field, exclude_unset=True must keep it out of
        the dump — otherwise openrag's Pydantic defaults (e.g. temperature=0.3,
        max_tokens=…) get forwarded and overrule upstream provider defaults."""
        req = OpenAIChatCompletionRequest.model_validate({"messages": [{"role": "user", "content": "hi"}]})
        dump = req.model_dump(exclude_unset=True)

        assert "temperature" not in dump
        assert "top_p" not in dump
        assert "max_tokens" not in dump
        assert "stream" not in dump
        assert "logprobs" not in dump
        assert "top_logprobs" not in dump

    def test_chat_request_preserves_explicit_fields(self):
        req = OpenAIChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.5,
                "stream": True,
            }
        )
        dump = req.model_dump(exclude_unset=True)

        assert dump["temperature"] == 0.5
        assert dump["stream"] is True


class TestChatCompletionRequestLogprobs:
    """Guard against regressing logprobs forwarding to OpenAI's chat completions API.

    OpenAI's chat completions API expects logprobs as a boolean (with a separate
    `top_logprobs` integer). If we type it as int, Pydantic v2 silently coerces
    True -> 1 and OpenAI rejects with 'expected boolean, but got integer'.
    """

    def test_logprobs_true_stays_bool(self):
        req = OpenAIChatCompletionRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            logprobs=True,
        )
        assert req.logprobs is True
        assert isinstance(req.logprobs, bool)

    def test_logprobs_false_stays_bool(self):
        req = OpenAIChatCompletionRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            logprobs=False,
        )
        assert req.logprobs is False
        assert isinstance(req.logprobs, bool)

    def test_logprobs_default_is_none(self):
        req = OpenAIChatCompletionRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert req.logprobs is None
        assert req.top_logprobs is None

    def test_top_logprobs_accepted(self):
        req = OpenAIChatCompletionRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            logprobs=True,
            top_logprobs=5,
        )
        assert req.top_logprobs == 5
