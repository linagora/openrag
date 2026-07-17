"""Unit tests for token validation logic in :mod:`api.routers.user.chat`.

The test file lives at openrag/ (not under api/routers/user/) because a
file named ``routers/test_*.py`` would shadow the third-party
``openai`` package during import resolution, causing a circular import.
"""

from unittest.mock import patch

import pytest

# Prevent Ray from scanning the working directory (which may contain
# permission-restricted folders like db/).
import ray  # noqa: E402

if not ray.is_initialized():
    ray.init(runtime_env={"working_dir": None}, ignore_reinit_error=True)

from api.routers.user.chat import validate_tokens_limit  # noqa: E402
from api.schemas.user.chat import OpenAIChatCompletionRequest, OpenAICompletionRequest  # noqa: E402


def fake_length_function(text: str) -> int:
    """Deterministic token counter: one token per whitespace-separated word."""
    return len(text.split())


@pytest.fixture(autouse=True)
def _mock_get_num_tokens():
    with patch("api.routers.user.chat.get_num_tokens", return_value=fake_length_function):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chat_request(content: str, max_tokens: int = 1024) -> OpenAIChatCompletionRequest:
    return OpenAIChatCompletionRequest(
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
    )


def _completion_request(prompt: str, max_tokens: int = 512) -> OpenAICompletionRequest:
    return OpenAICompletionRequest(
        prompt=prompt,
        max_tokens=max_tokens,
    )


# ---------------------------------------------------------------------------
# Parametrized: both request types
# ---------------------------------------------------------------------------


class TestValidateTokensLimit:
    """Tests for validate_tokens_limit()."""

    @pytest.mark.parametrize(
        "request_factory, content_tokens, max_tokens, limit, expected_valid",
        [
            # Chat: well under limit
            (_chat_request, 10, 100, 500, True),
            # Chat: exactly at limit (tokens + 4 overhead per message + max_tokens)
            (_chat_request, 10, 100, 114, True),
            # Chat: one over limit
            (_chat_request, 10, 100, 113, False),
            # Completion: well under limit
            (_completion_request, 10, 50, 500, True),
            # Completion: exactly at limit
            (_completion_request, 10, 50, 60, True),
            # Completion: one over limit
            (_completion_request, 10, 50, 59, False),
        ],
        ids=[
            "chat-under",
            "chat-exact",
            "chat-over",
            "completion-under",
            "completion-exact",
            "completion-over",
        ],
    )
    def test_boundary(self, request_factory, content_tokens, max_tokens, limit, expected_valid):
        content = " ".join(["word"] * content_tokens)
        req = request_factory(content, max_tokens)
        is_valid, error_message = validate_tokens_limit(req, max_tokens_allowed=limit)
        assert is_valid is expected_valid
        if not expected_valid:
            assert "exceeds maximum token limit" in error_message.lower()

    def test_chat_default_max_tokens(self):
        """When max_tokens is not set, the default (1024) is used."""
        req = OpenAIChatCompletionRequest(
            messages=[{"role": "user", "content": "hello"}],
        )
        # 1 word + 4 overhead + 1024 default = 1029
        is_valid, _ = validate_tokens_limit(req, max_tokens_allowed=1029)
        assert is_valid is True
        is_valid, _ = validate_tokens_limit(req, max_tokens_allowed=1028)
        assert is_valid is False

    def test_completion_default_max_tokens(self):
        """When max_tokens is not set, the default (1024) is used."""
        req = OpenAICompletionRequest(prompt="hello")
        # 1 word + 1024 default = 1025
        is_valid, _ = validate_tokens_limit(req, max_tokens_allowed=1025)
        assert is_valid is True
        is_valid, _ = validate_tokens_limit(req, max_tokens_allowed=1024)
        assert is_valid is False

    def test_error_message_contains_token_counts(self):
        req = _chat_request("one two three", max_tokens=100)
        is_valid, msg = validate_tokens_limit(req, max_tokens_allowed=10)
        assert is_valid is False
        # 3 content tokens + 4 overhead = 7 message tokens
        assert "7" in msg  # message tokens (content + overhead)
        assert "100" in msg  # requested tokens
        assert "10" in msg  # max allowed

    def test_graceful_on_exception(self):
        """When get_num_tokens raises, validation returns True (graceful skip)."""
        with patch("api.routers.user.chat.get_num_tokens", side_effect=RuntimeError("boom")):
            req = _chat_request("hello", max_tokens=999999)
            is_valid, msg = validate_tokens_limit(req, max_tokens_allowed=1)
            assert is_valid is True
            assert msg == ""


# ---------------------------------------------------------------------------
# Per-endpoint token budgets (admin-configurable, default-endpoint sourced)
# ---------------------------------------------------------------------------


def _settings_with_default_llm(**extra):
    """Fresh Settings whose default LLM endpoint carries the given extra keys."""
    from core.config.model_endpoints import ModelEndpointConfig
    from core.config.root import Settings

    s = Settings()
    s.models.llm["default"] = ModelEndpointConfig(endpoint="http://llm:8000/v1", extra=dict(extra))
    return s


class TestEndpointConfiguredOutputTokens:
    """validate_tokens_limit uses the default LLM endpoint's max_output_tokens
    as the fallback when the request doesn't set max_tokens."""

    def test_uses_endpoint_output_tokens_default(self):
        from core.config.model_endpoints import LLM_OUTPUT_TOKENS_KEY

        s = _settings_with_default_llm(**{LLM_OUTPUT_TOKENS_KEY: 2048})
        # Explicit None bypasses the schema default_factory so the fallback runs.
        req = OpenAIChatCompletionRequest(messages=[{"role": "user", "content": "hello"}], max_tokens=None)
        # 1 word + 4 overhead + 2048 endpoint default = 2053
        assert validate_tokens_limit(req, max_tokens_allowed=2053, settings=s)[0] is True
        assert validate_tokens_limit(req, max_tokens_allowed=2052, settings=s)[0] is False

    def test_falls_back_to_global_when_endpoint_has_no_override(self):
        s = _settings_with_default_llm()  # no override → global default (1024)
        req = OpenAIChatCompletionRequest(messages=[{"role": "user", "content": "hello"}], max_tokens=None)
        # 1 + 4 + 1024 global default = 1029
        assert validate_tokens_limit(req, max_tokens_allowed=1029, settings=s)[0] is True
        assert validate_tokens_limit(req, max_tokens_allowed=1028, settings=s)[0] is False


class TestGetMaxModelTokens:
    """get_max_model_tokens precedence: endpoint config > startup-primed > global."""

    def test_endpoint_config_wins_over_primed(self, monkeypatch):
        import api.routers.user.chat as chat
        from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY

        s = _settings_with_default_llm(**{LLM_CONTEXT_SIZE_KEY: 32768})
        monkeypatch.setattr(chat, "load_config", lambda: s)
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {"default": 8000})  # auto-probed value present
        assert chat.get_max_model_tokens() == 32768

    def test_primed_used_when_no_endpoint_config(self, monkeypatch):
        import api.routers.user.chat as chat

        s = _settings_with_default_llm()  # no override
        monkeypatch.setattr(chat, "load_config", lambda: s)
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {"default": 8000})
        assert chat.get_max_model_tokens() == 8000

    def test_global_fallback_when_nothing_configured(self, monkeypatch):
        import api.routers.user.chat as chat

        s = _settings_with_default_llm()  # no override
        monkeypatch.setattr(chat, "load_config", lambda: s)
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})
        assert chat.get_max_model_tokens() == int(s.llm_context.max_llm_context_size)


class _FakeOpenAIModel:
    """Minimal stand-in for the openai SDK's Model object — needs .id and
    either .model_dump() or .dict() reporting max_model_len."""

    def __init__(self, id: str, max_model_len: int):
        self.id = id
        self._max_model_len = max_model_len

    def model_dump(self):
        return {"id": self.id, "max_model_len": self._max_model_len}


class TestPrimeMaxModelTokens:
    """prime_max_model_tokens probes every registered LLM endpoint's
    /v1/models for max_model_len — not just the default — caching per
    endpoint name so a partition's chat_llm preset gets its own auto-probed
    budget (the '639' fix)."""

    async def test_probes_each_distinct_endpoint_and_caches_by_name(self, monkeypatch):
        import api.routers.user.chat as chat
        from core.config.model_endpoints import ModelEndpointConfig
        from core.config.root import Settings

        s = Settings()
        s.models.llm["default"] = ModelEndpointConfig(endpoint="http://default:8000/v1", model_name="model-a")
        s.models.llm["mistral"] = ModelEndpointConfig(endpoint="http://mistral:8000/v1", model_name="model-b")
        calls = []

        async def fake_get_openai_models(base_url, api_key, timeout=30):
            calls.append(base_url)
            return {
                "http://default:8000/v1": [_FakeOpenAIModel("model-a", 8192)],
                "http://mistral:8000/v1": [_FakeOpenAIModel("model-b", 32768)],
            }[base_url]

        monkeypatch.setattr(chat, "get_openai_models", fake_get_openai_models)
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})

        await chat.prime_max_model_tokens(s)

        assert chat._max_model_tokens_by_name == {"default": 8192, "mistral": 32768}
        assert sorted(calls) == ["http://default:8000/v1", "http://mistral:8000/v1"]

    async def test_dedupes_aliased_default_endpoint(self, monkeypatch):
        """The "default" name always aliases whichever endpoint is is_default
        (same ModelEndpointConfig object) — it must be probed once, and the
        result cached under both names."""
        import api.routers.user.chat as chat
        from core.config.model_endpoints import ModelEndpointConfig
        from core.config.root import Settings

        s = Settings()
        shared = ModelEndpointConfig(endpoint="http://default:8000/v1", model_name="model-a")
        s.models.llm["default"] = shared
        s.models.llm["model-a"] = shared
        calls = []

        async def fake_get_openai_models(base_url, api_key, timeout=30):
            calls.append(base_url)
            return [_FakeOpenAIModel("model-a", 8192)]

        monkeypatch.setattr(chat, "get_openai_models", fake_get_openai_models)
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})

        await chat.prime_max_model_tokens(s)

        assert chat._max_model_tokens_by_name == {"default": 8192, "model-a": 8192}
        assert calls == ["http://default:8000/v1"]

    async def test_endpoint_without_model_name_is_skipped(self, monkeypatch):
        import api.routers.user.chat as chat
        from core.config.model_endpoints import ModelEndpointConfig
        from core.config.root import Settings

        s = Settings()
        s.models.llm["default"] = ModelEndpointConfig(endpoint="http://default:8000/v1")  # no model_name

        async def fake_get_openai_models(base_url, api_key, timeout=30):
            pytest.fail("must not probe an endpoint with no model_name")

        monkeypatch.setattr(chat, "get_openai_models", fake_get_openai_models)
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})

        await chat.prime_max_model_tokens(s)

        assert chat._max_model_tokens_by_name == {}

    async def test_one_endpoint_probe_failure_does_not_affect_others(self, monkeypatch):
        import api.routers.user.chat as chat
        from core.config.model_endpoints import ModelEndpointConfig
        from core.config.root import Settings

        s = Settings()
        s.models.llm["default"] = ModelEndpointConfig(endpoint="http://default:8000/v1", model_name="model-a")
        s.models.llm["broken"] = ModelEndpointConfig(endpoint="http://broken:8000/v1", model_name="model-b")

        async def fake_get_openai_models(base_url, api_key, timeout=30):
            if base_url == "http://broken:8000/v1":
                raise RuntimeError("connection refused")
            return [_FakeOpenAIModel("model-a", 8192)]

        monkeypatch.setattr(chat, "get_openai_models", fake_get_openai_models)
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})

        await chat.prime_max_model_tokens(s)

        assert chat._max_model_tokens_by_name == {"default": 8192}


def _partition_config(chat_llm=None):
    from core.config.indexation_pipeline import IndexationPipelineConfig
    from core.config.retrieval_pipeline import RetrievalPipelineConfig
    from core.models.preset import PartitionConfig

    return PartitionConfig(
        name="p",
        indexation=IndexationPipelineConfig(),
        retrieval=RetrievalPipelineConfig(),
        chat_llm=chat_llm,
    )


class TestPartitionResolvedMaxModelTokens:
    """get_max_model_tokens resolves the partition's chat_llm preset endpoint
    over the default when the request is scoped to a partition that sets one —
    the '639' fix: checking against the LLM that will actually answer, not
    always the global default."""

    def test_partition_preset_endpoint_budget_wins_over_default(self, monkeypatch):
        import api.routers.user.chat as chat
        from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY, LLM_OUTPUT_TOKENS_KEY, ModelEndpointConfig

        s = _settings_with_default_llm(**{LLM_CONTEXT_SIZE_KEY: 8192})
        s.models.llm["mistral"] = ModelEndpointConfig(
            endpoint="http://mistral:8000/v1",
            extra={LLM_CONTEXT_SIZE_KEY: 32768, LLM_OUTPUT_TOKENS_KEY: 4096},
        )
        s.partitions["p"] = _partition_config(chat_llm="mistral")
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {"default": 8000, "mistral": 16000})

        assert chat.get_max_model_tokens(partitions=["p"], settings=s) == 32768
        # No partition (direct-LLM) still resolves the default endpoint.
        assert chat.get_max_model_tokens(partitions=None, settings=s) == 8192

    def test_partition_preset_uses_its_own_probed_value(self, monkeypatch):
        """Each endpoint's auto-probed max_model_len is cached under its own
        name (prime_max_model_tokens probes every registered LLM endpoint,
        not just the default) — a partition preset with no admin override
        gets its own probed budget, not the default endpoint's."""
        import api.routers.user.chat as chat
        from core.config.model_endpoints import ModelEndpointConfig

        s = _settings_with_default_llm()  # no explicit context size configured
        s.models.llm["mistral"] = ModelEndpointConfig(endpoint="http://mistral:8000/v1")
        s.partitions["p"] = _partition_config(chat_llm="mistral")
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {"default": 8000, "mistral": 32768})

        assert chat.get_max_model_tokens(partitions=["p"], settings=s) == 32768
        assert chat.get_max_model_tokens(partitions=None, settings=s) == 8000

    def test_unprobed_preset_falls_back_to_global_not_default_probed_value(self, monkeypatch):
        """A resolved endpoint with neither an admin override nor a
        successful probe of its own must not silently borrow the default
        endpoint's cached probe — it falls straight to the global default."""
        import api.routers.user.chat as chat
        from core.config.model_endpoints import ModelEndpointConfig

        s = _settings_with_default_llm()  # no explicit context size configured
        s.models.llm["mistral"] = ModelEndpointConfig(endpoint="http://mistral:8000/v1")
        s.partitions["p"] = _partition_config(chat_llm="mistral")
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {"default": 8000})  # "mistral" never probed

        assert chat.get_max_model_tokens(partitions=["p"], settings=s) == int(s.llm_context.max_llm_context_size)
        assert chat.get_max_model_tokens(partitions=None, settings=s) == 8000

    def test_conflicting_partition_presets_fall_back_to_default(self, monkeypatch):
        import api.routers.user.chat as chat
        from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY, ModelEndpointConfig

        s = _settings_with_default_llm(**{LLM_CONTEXT_SIZE_KEY: 8192})
        s.models.llm["mistral"] = ModelEndpointConfig(
            endpoint="http://mistral:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: 32768}
        )
        s.models.llm["llama"] = ModelEndpointConfig(endpoint="http://llama:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: 4096})
        s.partitions["a"] = _partition_config(chat_llm="mistral")
        s.partitions["b"] = _partition_config(chat_llm="llama")
        monkeypatch.setattr(chat, "_max_model_tokens_by_name", {})

        assert chat.get_max_model_tokens(partitions=["a", "b"], settings=s) == 8192


class TestDefaultMaxTokensFactory:
    """The request-schema default_factory prefers the default endpoint's budget."""

    def test_prefers_endpoint_output_tokens(self, monkeypatch):
        import core.config
        from api.schemas.user.chat import default_max_tokens
        from core.config.model_endpoints import LLM_OUTPUT_TOKENS_KEY

        s = _settings_with_default_llm(**{LLM_OUTPUT_TOKENS_KEY: 4096})
        monkeypatch.setattr(core.config, "load_config", lambda: s)
        assert default_max_tokens() == 4096

    def test_global_fallback(self, monkeypatch):
        import core.config
        from api.schemas.user.chat import default_max_tokens

        s = _settings_with_default_llm()  # no override
        monkeypatch.setattr(core.config, "load_config", lambda: s)
        assert default_max_tokens() == s.llm_context.max_output_tokens
