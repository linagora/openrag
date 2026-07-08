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
        monkeypatch.setattr(chat, "_max_model_tokens", 8000)  # auto-probed value present
        assert chat.get_max_model_tokens() == 32768

    def test_primed_used_when_no_endpoint_config(self, monkeypatch):
        import api.routers.user.chat as chat

        s = _settings_with_default_llm()  # no override
        monkeypatch.setattr(chat, "load_config", lambda: s)
        monkeypatch.setattr(chat, "_max_model_tokens", 8000)
        assert chat.get_max_model_tokens() == 8000

    def test_global_fallback_when_nothing_configured(self, monkeypatch):
        import api.routers.user.chat as chat

        s = _settings_with_default_llm()  # no override
        monkeypatch.setattr(chat, "load_config", lambda: s)
        monkeypatch.setattr(chat, "_max_model_tokens", None)
        assert chat.get_max_model_tokens() == int(s.llm_context.max_llm_context_size)


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
