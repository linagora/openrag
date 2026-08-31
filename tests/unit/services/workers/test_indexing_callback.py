from __future__ import annotations

import json
import re
import socket
from datetime import datetime
from unittest import mock

import httpx
import pytest
from services.workers.indexing_callback import send_indexing_callback


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, *addresses: str, error: Exception | None = None) -> None:
    """``loop.getaddrinfo`` defers here, so this exercises the real path."""

    def _resolve(host, port, *_a, **_k):
        if error is not None:
            raise error
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port)) for addr in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off real DNS; the guard's own tests re-patch the resolver."""
    _patch_resolver(monkeypatch, "93.184.216.34")


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.workers.indexing_callback.httpx.AsyncClient", factory)


@pytest.fixture
def captured_body(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Captures the POST body in ``captured["body"]``; the transport answers 200."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    _patch_async_client(monkeypatch, handler)
    return captured


@pytest.fixture
def captured_request(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Like ``captured_body``, but keeps the request so headers can be asserted."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    _patch_async_client(monkeypatch, handler)
    return captured


@pytest.mark.asyncio
async def test_success_callback_echoes_file_rev_verbatim(captured_body: dict) -> None:
    """file_rev is cozy's CouchDB revision: opaque, and stale if transformed."""
    file_rev = "3-a1b2C3d4E5f6/g7+h8=="

    await send_indexing_callback(
        "https://cozy.example.com/rag/callback",
        "alice.mycozy.cloud",
        "file-123",
        "success",
        {"file_rev": file_rev, "datetime": "2026-01-01T00:00:00Z", "doctype": "io.cozy.files"},
    )

    body = captured_body["body"]
    assert body["partition"] == "alice.mycozy.cloud"
    assert body["file_id"] == "file-123"
    assert body["status"] == "success"
    assert body["metadata"]["file_rev"] == file_rev
    assert body["metadata"]["datetime"] == "2026-01-01T00:00:00Z"
    assert body["metadata"]["doctype"] == "io.cozy.files"
    # `timestamp` is generated at send time and must be a valid ISO8601 string.
    datetime.fromisoformat(body["timestamp"])


@pytest.mark.asyncio
async def test_error_callback_uses_error_status_and_echoes_metadata(captured_body: dict) -> None:
    await send_indexing_callback(
        "https://cozy.example.com/rag/callback",
        "p",
        "f1",
        "error",
        {"file_rev": "deadbeef"},
    )

    body = captured_body["body"]
    assert body["status"] == "error"
    assert body["metadata"]["file_rev"] == "deadbeef"
    assert body["metadata"]["datetime"] is None
    assert body["metadata"]["doctype"] is None


@pytest.mark.asyncio
async def test_no_callback_url_is_a_strict_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made when callback_url is None")

    _patch_async_client(monkeypatch, handler)

    await send_indexing_callback(None, "p", "f1", "success", {"file_rev": "abc"})


@pytest.mark.asyncio
async def test_unsafe_callback_url_is_skipped_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made to an unsafe callback_url")

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("http://127.0.0.1:9999/x", "p", "f1", "success", {"file_rev": "abc"})
        mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_callback_url_credentials_are_not_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made to an unsafe callback_url")

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://leakme:hunter2@127.0.0.1/callback", "p", "f1", "success", {"file_rev": "abc"}
        )
        mock_logger.warning.assert_called_once()
        logged_url = mock_logger.warning.call_args[1]["callback_url"]
        assert "leakme" not in logged_url
        assert "hunter2" not in logged_url


@pytest.mark.asyncio
async def test_callback_error_with_bad_port_and_query_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """urlparse accepts a non-numeric port; httpx.URL rejects it, inside the handler."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("should fail before an HTTP call is attempted")

    _patch_async_client(monkeypatch, handler)

    await send_indexing_callback(
        "https://cozy.example.com:abc/callback?token=SECRET", "p", "f1", "success", {"file_rev": "abc"}
    )


@pytest.mark.asyncio
async def test_callback_error_with_non_printable_char_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """urlparse tolerates a non-printable character; httpx.URL rejects it mid-flight."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - httpx rejects before dispatch
        raise AssertionError("should fail before a response is produced")

    _patch_async_client(monkeypatch, handler)

    await send_indexing_callback(
        "https://cozy.example.com/callback?token=x\x00y", "p", "f1", "success", {"file_rev": "abc"}
    )


@pytest.mark.asyncio
async def test_callback_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _patch_async_client(monkeypatch, handler)

    # Must not raise even though the remote endpoint returns an error.
    await send_indexing_callback(
        "https://cozy.example.com/rag/callback",
        "p",
        "f1",
        "success",
        {"file_rev": "abc"},
    )


@pytest.mark.asyncio
async def test_callback_error_redacts_query_string_from_logged_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx's HTTPStatusError message embeds the full request URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://cozy.example.com/rag/callback?token=SECRETVALUE",
            "p",
            "f1",
            "success",
            {"file_rev": "abc"},
        )

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[1]["callback_url"] == "https://cozy.example.com"
        assert "SECRETVALUE" not in call_args[1]["error"]


@pytest.mark.asyncio
async def test_malformed_callback_url_is_swallowed_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """urlparse raises on some inputs, e.g. an unterminated IPv6 literal."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made for a malformed callback_url")

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("http://[::1", "p", "f1", "success", {"file_rev": "abc"})
        mock_logger.warning.assert_called_once()
        assert "callback_url" in mock_logger.warning.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_callback_error_redacts_percent_encoded_query_from_logged_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx percent-encodes the query, so the message embeds the encoded form."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://cozy.example.com/rag/callback?token=SECRET VALUE!",
            "p",
            "f1",
            "success",
            {"file_rev": "abc"},
        )

        mock_logger.warning.assert_called_once()
        error_message = mock_logger.warning.call_args[1]["error"]
        assert "SECRET" not in error_message


@pytest.mark.asyncio
async def test_missing_metadata_defaults_to_none_fields(captured_body: dict) -> None:
    await send_indexing_callback("https://cozy.example.com/rag/callback", "p", "f1", "success", None)

    body = captured_body["body"]
    assert body["metadata"] == {"file_rev": None, "datetime": None, "doctype": None}


@pytest.mark.asyncio
async def test_missing_file_rev_is_sent_without_warning(captured_body: dict) -> None:
    """cozy-stack only compares a non-empty revision, so an empty one is current."""
    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://cozy.example.com/rag/callback", "p", "f1", "success", {"doctype": "io.cozy.files"}
        )

        mock_logger.warning.assert_not_called()

    # The callback is still sent even though the metadata is incomplete.
    assert captured_body["body"]["metadata"]["file_rev"] is None


@pytest.mark.asyncio
async def test_present_file_rev_does_not_log_warning(captured_body: dict) -> None:
    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("https://cozy.example.com/rag/callback", "p", "f1", "success", {"file_rev": "abc"})

        mock_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_callback_token_is_sent_as_bearer_header(captured_request: dict) -> None:
    await send_indexing_callback(
        "https://cozy.example.com/ai/index/status",
        "alice.mycozy.cloud",
        "file-123",
        "success",
        {"file_rev": "abc"},
        callback_token="jwt-abc.def.ghi",
    )

    request = captured_request["request"]
    assert request.headers["Authorization"] == "Bearer jwt-abc.def.ghi"


@pytest.mark.asyncio
async def test_callback_token_never_reaches_url_or_payload(captured_request: dict) -> None:
    await send_indexing_callback(
        "https://cozy.example.com/ai/index/status",
        "p",
        "f1",
        "success",
        {"file_rev": "abc"},
        callback_token="s3cret-token",
    )

    request = captured_request["request"]
    assert "s3cret-token" not in str(request.url)
    assert "s3cret-token" not in request.content.decode()
    assert json.loads(request.content).keys() == {"partition", "file_id", "status", "timestamp", "metadata"}


@pytest.mark.asyncio
async def test_no_callback_token_sends_no_authorization_header(captured_request: dict) -> None:
    """Back-compat: an unauthenticated endpoint sees the request it saw before."""
    await send_indexing_callback("https://cozy.example.com/rag/callback", "p", "f1", "success", {"file_rev": "abc"})

    assert "Authorization" not in captured_request["request"].headers


@pytest.mark.asyncio
async def test_callback_token_is_redacted_from_logged_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream refused Bearer s3cret-token")

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://cozy.example.com/ai/index/status",
            "p",
            "f1",
            "success",
            {"file_rev": "abc"},
            callback_token="s3cret-token",
        )

        mock_logger.warning.assert_called_once()
        assert "s3cret-token" not in mock_logger.warning.call_args[1]["error"]
        assert "REDACTED" in mock_logger.warning.call_args[1]["error"]


@pytest.mark.asyncio
async def test_private_callback_url_is_sent_when_operator_opts_in(
    monkeypatch: pytest.MonkeyPatch, captured_request: dict
) -> None:
    monkeypatch.setattr("services.workers.indexing_callback._allow_private_callback_urls", lambda: True)

    await send_indexing_callback("http://localhost:8080/ai/index/status", "p", "f1", "success", {"file_rev": "abc"})

    assert str(captured_request["request"].url) == "http://localhost:8080/ai/index/status"


@pytest.mark.asyncio
async def test_private_callback_url_still_blocked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made to a private callback_url by default")

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("http://localhost:8080/ai/index/status", "p", "f1", "success", {"file_rev": "abc"})
        mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_opting_in_to_private_urls_still_rejects_non_http_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.workers.indexing_callback._allow_private_callback_urls", lambda: True)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made for a non-http(s) scheme")

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("file:///etc/passwd", "p", "f1", "success", {"file_rev": "abc"})
        mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_allow_private_callback_urls_defaults_to_false_on_config_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.workers import indexing_callback

    def boom() -> None:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(indexing_callback, "load_config", boom)
    assert indexing_callback._allow_private_callback_urls() is False


@pytest.mark.asyncio
async def test_url_credentials_are_redacted_from_logged_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx echoes the full URL in its exception messages, which we log verbatim."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://leakme:hunter2@cozy.example.com/cb", "p", "f1", "success", {"file_rev": "abc"}
        )

        mock_logger.warning.assert_called_once()
        logged = mock_logger.warning.call_args[1]
        assert "hunter2" not in logged["error"]
        assert "leakme" not in logged["error"]
        assert "hunter2" not in logged["callback_url"]
        # The host is still there: redaction must not cost us the diagnostics.
        assert "cozy.example.com" in logged["error"]


@pytest.mark.asyncio
async def test_url_credentials_with_reserved_chars_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx percent-encodes, so the password appears in a spelling urlparse never produced."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://user:p%40ss word@cozy.example.com/cb", "p", "f1", "success", {"file_rev": "abc"}
        )

        mock_logger.warning.assert_called_once()
        error = mock_logger.warning.call_args[1]["error"]
        assert "p%40ss" not in error
        assert "p@ss" not in error


@pytest.mark.asyncio
async def test_username_redaction_does_not_leave_password_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://bob:bobsecret@cozy.example.com/cb", "p", "f1", "success", {"file_rev": "abc"}
        )

        error = mock_logger.warning.call_args[1]["error"]
        assert "bobsecret" not in error


@pytest.mark.asyncio
async def test_public_hostname_resolving_internally_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made to a host resolving internally")

    _patch_async_client(monkeypatch, handler)
    _patch_resolver(monkeypatch, "169.254.169.254")

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("https://evil.example.com/cb", "p", "f1", "success", {"file_rev": "abc"})
        mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_one_internal_record_among_public_ones_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made when any record is internal")

    _patch_async_client(monkeypatch, handler)
    _patch_resolver(monkeypatch, "93.184.216.34", "10.0.0.5")

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("https://cozy.example.com/cb", "p", "f1", "success", {"file_rev": "abc"})
        mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_publicly_resolving_hostname_is_sent(monkeypatch: pytest.MonkeyPatch, captured_body: dict) -> None:
    _patch_resolver(monkeypatch, "93.184.216.34")

    await send_indexing_callback("https://cozy.example.com/cb", "p", "f1", "success", {"file_rev": "abc"})

    assert captured_body["body"]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_unresolvable_hostname_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made for an unresolvable host")

    _patch_async_client(monkeypatch, handler)

    _patch_resolver(monkeypatch, error=socket.gaierror("nope"))

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback("https://nowhere.example.com/cb", "p", "f1", "success", {"file_rev": "abc"})
        mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_opting_in_to_private_urls_skips_resolution(monkeypatch: pytest.MonkeyPatch, captured_body: dict) -> None:
    monkeypatch.setattr("services.workers.indexing_callback._allow_private_callback_urls", lambda: True)

    _patch_resolver(monkeypatch, error=AssertionError("resolution must be skipped for private URLs"))

    await send_indexing_callback("http://cozy.localhost:8080/ai/index/status", "p", "f1", "success", {"file_rev": "a"})

    assert captured_body["body"]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_tokenized_http_callback_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made for a tokenized http:// callback")

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "http://cozy.example.com/ai/index/status",
            "p",
            "f1",
            "success",
            {"file_rev": "abc"},
            callback_token="s3cret-token",
        )

        mock_logger.warning.assert_called_once()
        # The refusal itself must not echo the token it is protecting.
        assert "s3cret-token" not in str(mock_logger.warning.call_args)


@pytest.mark.asyncio
async def test_untokenized_http_callback_is_still_sent(captured_body: dict) -> None:
    """Back-compat: only token-bearing callbacks gain the https requirement."""
    await send_indexing_callback("http://cozy.example.com/rag/callback", "p", "f1", "success", {"file_rev": "abc"})

    assert captured_body["body"]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_tokenized_https_callback_is_unaffected(captured_request: dict) -> None:
    await send_indexing_callback(
        "https://cozy.example.com/ai/index/status", "p", "f1", "success", {"file_rev": "abc"}, callback_token="jwt"
    )

    assert captured_request["request"].headers["Authorization"] == "Bearer jwt"


@pytest.mark.asyncio
async def test_tokenized_http_callback_allowed_by_the_dev_opt_in(
    monkeypatch: pytest.MonkeyPatch, captured_request: dict
) -> None:
    monkeypatch.setattr("services.workers.indexing_callback._allow_private_callback_urls", lambda: True)

    await send_indexing_callback(
        "http://cozy.localhost:8080/ai/index/status", "p", "f1", "success", {"file_rev": "abc"}, callback_token="jwt"
    )

    assert captured_request["request"].headers["Authorization"] == "Bearer jwt"


@pytest.mark.asyncio
async def test_timestamp_is_rfc3339_with_z_and_milliseconds(captured_body: dict) -> None:
    await send_indexing_callback("https://cozy.example.com/rag/callback", "p", "f1", "success", {"file_rev": "3-abc"})

    timestamp = captured_body["body"]["timestamp"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", timestamp), timestamp
    # Still round-trips: a "Z" suffix is accepted from Python 3.11 on.
    assert datetime.fromisoformat(timestamp).tzinfo is not None


@pytest.mark.asyncio
async def test_a_secret_in_the_callback_path_is_not_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook secrets conventionally live in the path, not the query."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _patch_async_client(monkeypatch, handler)

    with mock.patch("services.workers.indexing_callback.logger") as mock_logger:
        await send_indexing_callback(
            "https://cozy.example.com/hooks/T000/B000/SECRETPATH", "p", "f1", "success", {"file_rev": "abc"}
        )

        mock_logger.warning.assert_called_once()
        logged = mock_logger.warning.call_args[1]
        assert "SECRETPATH" not in logged["callback_url"]
        assert "SECRETPATH" not in logged["error"]
        # The host is still there: redaction must not cost us the diagnostics.
        assert "cozy.example.com" in logged["error"]
