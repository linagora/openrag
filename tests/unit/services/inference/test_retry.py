import httpx
import pytest
from core.utils.exceptions import OpenRAGError, ServiceUnavailableError
from services.inference._retry import _is_retryable, with_retry


class TestIsRetryable:
    def test_timeout_exception(self):
        assert _is_retryable(httpx.ReadTimeout("timeout"))

    def test_connect_error(self):
        assert _is_retryable(httpx.ConnectError("refused"))

    @pytest.mark.parametrize("status", [429, 502, 503, 504])
    def test_retryable_http_status(self, status):
        req = httpx.Request("GET", "http://test")
        exc = httpx.HTTPStatusError("err", request=req, response=httpx.Response(status, request=req))
        assert _is_retryable(exc)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
    def test_non_retryable_http_status(self, status):
        req = httpx.Request("GET", "http://test")
        exc = httpx.HTTPStatusError("err", request=req, response=httpx.Response(status, request=req))
        assert not _is_retryable(exc)

    def test_openrag_error_retryable(self):
        assert _is_retryable(ServiceUnavailableError("down"))  # 503

    def test_openrag_error_non_retryable(self):
        assert not _is_retryable(OpenRAGError("bad", status_code=404))

    def test_unrelated_exception(self):
        assert not _is_retryable(ValueError("nope"))


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        call_count = 0

        @with_retry(max_attempts=3, base_wait=0.01, max_wait=0.1)
        async def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await succeed()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_connect_error(self):
        call_count = 0

        @with_retry(max_attempts=3, base_wait=0.01, max_wait=0.1)
        async def fail_connect():
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("refused")

        with pytest.raises(httpx.ConnectError):
            await fail_connect()

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        call_count = 0
        req = httpx.Request("GET", "http://test")

        @with_retry(max_attempts=3, base_wait=0.01, max_wait=0.1)
        async def fail_429():
            nonlocal call_count
            call_count += 1
            raise httpx.HTTPStatusError("rate limited", request=req, response=httpx.Response(429, request=req))

        with pytest.raises(httpx.HTTPStatusError):
            await fail_429()

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_400(self):
        call_count = 0
        req = httpx.Request("GET", "http://test")

        @with_retry(max_attempts=3, base_wait=0.01, max_wait=0.1)
        async def fail_400():
            nonlocal call_count
            call_count += 1
            raise httpx.HTTPStatusError("bad request", request=req, response=httpx.Response(400, request=req))

        with pytest.raises(httpx.HTTPStatusError):
            await fail_400()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_succeeds_after_transient_failure(self):
        call_count = 0

        @with_retry(max_attempts=3, base_wait=0.01, max_wait=0.1)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.ConnectError("transient")
            return "recovered"

        result = await flaky()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_openrag_503(self):
        call_count = 0

        @with_retry(max_attempts=3, base_wait=0.01, max_wait=0.1)
        async def fail_503():
            nonlocal call_count
            call_count += 1
            raise ServiceUnavailableError("down")

        with pytest.raises(ServiceUnavailableError):
            await fail_503()

        assert call_count == 3
