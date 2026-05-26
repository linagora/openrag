"""Unit tests for token validation logic in openrag.routers.openai.

The test file lives at openrag/ (not openrag/routers/) because a file
named ``routers/test_*.py`` would shadow the ``openai`` package during
import resolution, causing a circular import.
"""

from unittest.mock import patch

import pytest

# Prevent Ray from scanning the working directory (which may contain
# permission-restricted folders like db/).
import ray  # noqa: E402

if not ray.is_initialized():
    ray.init(runtime_env={"working_dir": None}, ignore_reinit_error=True)

from models.openai import OpenAIChatCompletionRequest, OpenAICompletionRequest  # noqa: E402
from routers.openai import validate_tokens_limit  # noqa: E402


def fake_length_function(text: str) -> int:
    """Deterministic token counter: one token per whitespace-separated word."""
    return len(text.split())


@pytest.fixture(autouse=True)
def _mock_get_num_tokens():
    with patch("routers.openai.get_num_tokens", return_value=fake_length_function):
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
        with patch("routers.openai.get_num_tokens", side_effect=RuntimeError("boom")):
            req = _chat_request("hello", max_tokens=999999)
            is_valid, msg = validate_tokens_limit(req, max_tokens_allowed=1)
            assert is_valid is True
            assert msg == ""
