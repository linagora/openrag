"""
Tests for external resource error detection utilities.
Related to: https://github.com/linagora/openrag/issues/182
"""

import pytest

from utils.external_resource_errors import is_external_resource_error


class TestIsExternalResourceError:
    """Test suite for is_external_resource_error function."""

    @pytest.mark.parametrize(
        "error_msg,expected_code,url_contains",
        [
            # Issue #182
            (
                "aiohttp.client_exceptions.ClientResponseError: 403, message='Forbidden', "
                "url='https://upload.wikimedia.org/wikipedia/commons/thumb/d/d5/Logo.png'",
                "403",
                "upload.wikimedia.org",
            ),
            # Other HTTP status codes
            (
                "ClientResponseError: 404, url='https://example.com/missing.png'",
                "404",
                "example.com",
            ),
            (
                "HTTPError: 401 Unauthorized for url: https://api.example.com/image.jpg",
                "401",
                "api.example.com",
            ),
            (
                "ClientResponseError: 429 Too Many Requests - https://cdn.example.com/img.png",
                "429",
                "cdn.example.com",
            ),
            # 5xx gateway errors
            (
                "502 Bad Gateway: https://api.example.com/image.png",
                "502",
                "api.example.com",
            ),
            (
                "ClientResponseError: 503 Service Unavailable - https://cdn.example.com/img.png",
                "503",
                "cdn.example.com",
            ),
            # vLLM wrapped error (the real-world scenario)
            (
                "openai.InternalServerError: Error code: 500 - {'error': {'message': "
                "'litellm.InternalServerError: aiohttp.client_exceptions.ClientResponseError: "
                "403, message=Forbidden, url=https://example.com/path/to/image.png'}}",
                "403",
                "example.com/path/to/image.png",
            ),
        ],
    )
    def test_detects_http_errors_with_urls(self, error_msg, expected_code, url_contains):
        """Test detection of HTTP errors with URL extraction."""
        is_external, status_code, url = is_external_resource_error(Exception(error_msg))

        assert is_external is True
        assert status_code == expected_code
        assert url_contains in url

    @pytest.mark.parametrize(
        "error_msg",
        [
            "TimeoutError: Connection timed out while fetching resource",
            "SSLError: Certificate verification failed",
            "ConnectionError: Failed to connect to server",
            "aiohttp.client_exceptions.ClientResponseError: some error",
            "requests.exceptions.HTTPError: 500 Server Error",
        ],
    )
    def test_detects_error_indicators(self, error_msg):
        """Test detection via error type indicators."""
        is_external, _, _ = is_external_resource_error(Exception(error_msg))
        assert is_external is True

    @pytest.mark.parametrize(
        "error",
        [
            Exception("ValueError: Invalid input parameter"),
            Exception("Something went wrong during processing"),
            TypeError("'NoneType' object is not subscriptable"),
            AttributeError("'dict' object has no attribute 'content'"),
            Exception(""),
            # vLLM error without external cause details
            Exception(
                "openai.InternalServerError: Error code: 500 - {'error': {'message': "
                "'litellm.InternalServerError: InternalServerError: OpenAIException'}}"
            ),
        ],
    )
    def test_does_not_flag_internal_errors(self, error):
        """Test that internal/generic errors are not flagged as external."""
        is_external, status_code, url = is_external_resource_error(error)

        assert is_external is False
        assert status_code == ""
        assert url == ""

    def test_extracts_url_with_query_params(self):
        """Test URL extraction with query parameters."""
        error = Exception("403 Forbidden: https://api.example.com/image?id=123&size=large")
        _, _, url = is_external_resource_error(error)

        assert "api.example.com/image?id=123" in url

    def test_indicator_substring_causes_false_positive(self):
        """Document known limitation: indicator substrings cause false positives.

        This test documents that internal errors mentioning HTTP error class names
        will be incorrectly classified as external. This is accepted because:
        1. Real error messages use these as exception class names, not prose
        2. Stricter matching (word boundaries) would break legitimate matches
           like 'aiohttp.client_exceptions.ClientResponseError'
        3. This scenario is unlikely in practice
        """
        error = Exception("InternalServerError: Failed to handle ClientResponseError in retry logic")
        is_external, status_code, url = is_external_resource_error(error)

        # This IS classified as external (false positive) due to substring match
        assert is_external is True
        assert status_code == ""  # No HTTP status code
        assert url == ""  # No URL
