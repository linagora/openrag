import asyncio
import socket
from pathlib import Path

import httpx
import pytest
from services.websearch.base import WebResult
from services.websearch.content_fetcher import ContentFetcher, _is_safe_url


@pytest.fixture
def fetcher():
    return ContentFetcher(max_results=3, timeout=1.0, max_tokens_per_page=500)


def _make_result(url="https://example.com", snippet="short snippet"):
    return WebResult(title="Test", url=url, snippet=snippet)


# ---------------------------------------------------------------------------
# _is_safe_url — unit tests (no network, no client)
# ---------------------------------------------------------------------------


class TestIsSafeUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/secret",
            "http://127.0.0.1/admin",
            "http://127.0.0.42/x",
            "http://[::1]/admin",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/metadata",  # AWS/cloud metadata service
            "http://0.0.0.0/x",
            "http://100.64.0.1/cgnat",  # RFC 6598 shared address space
            "http://198.18.0.1/bench",  # Benchmarking range
        ],
    )
    def test_blocks_private_and_reserved_addresses(self, url):
        assert _is_safe_url(url) is False

    def test_blocks_decimal_encoded_loopback(self):
        """2130706433 is 127.0.0.1 in decimal integer form."""
        assert _is_safe_url("http://2130706433/secret") is False

    def test_blocks_decimal_encoded_private(self):
        """167772161 is 10.0.0.1 in decimal integer form."""
        assert _is_safe_url("http://167772161/secret") is False

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "data:text/html,<h1>hi</h1>",
            "javascript:alert(1)",
        ],
    )
    def test_blocks_non_http_schemes(self, url):
        assert _is_safe_url(url) is False

    def test_allows_public_ip(self):
        # 93.184.216.34 is example.com — globally routable
        assert _is_safe_url("http://93.184.216.34/page") is True

    def test_allows_regular_hostname(self):
        assert _is_safe_url("https://example.com/page") is True


# ---------------------------------------------------------------------------
# _fetch_single — integration tests with mock transport
# ---------------------------------------------------------------------------


class TestFetchSingleURL:
    @pytest.mark.asyncio
    async def test_extracts_text_from_html(self, fetcher):
        html = "<html><body><h1>Hello</h1><p>World paragraph content here.</p></body></html>"

        async def mock_handler(request):
            return httpx.Response(200, text=html)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, "https://example.com")

        assert text is not None
        assert "Hello" in text
        assert "World paragraph content" in text

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self, fetcher):
        async def slow_handler(request):
            await asyncio.sleep(5)
            return httpx.Response(200, text="too late")

        transport = httpx.MockTransport(slow_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, "https://slow.example.com")

        assert text is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self, fetcher):
        async def error_handler(request):
            return httpx.Response(500, text="error")

        transport = httpx.MockTransport(error_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, "https://error.example.com")

        assert text is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/secret",
            "http://127.0.0.1/admin",
            "http://127.0.0.42/x",
            "http://[::1]/admin",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/metadata",
            "http://0.0.0.0/x",
        ],
    )
    async def test_skips_loopback_urls(self, fetcher, url):
        async def mock_handler(request):
            return httpx.Response(200, text="<html><body>secret</body></html>")

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, url)

        assert text is None

    @pytest.mark.asyncio
    async def test_blocks_redirect_to_private_ip(self, fetcher):
        """A response that redirects to a private IP must be blocked after the initial fetch."""
        calls: list[str] = []

        async def redirecting_handler(request):
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(
                    302,
                    headers={"location": "http://192.168.1.1/secret"},
                )
            return httpx.Response(200, text="<html><body>SSRF data</body></html>")

        transport = httpx.MockTransport(redirecting_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            # 93.184.216.34 is example.com — passes the initial check
            text = await fetcher._fetch_single(client, "http://93.184.216.34/page")

        assert text is None
        # The redirect target (192.168.1.1) must never have been contacted
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_follows_safe_redirect(self, fetcher):
        """A redirect to another public URL must be followed and its content returned."""
        calls: list[str] = []

        async def handler(request):
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(
                    301,
                    headers={"location": "http://93.184.216.34/final"},
                )
            return httpx.Response(
                200,
                text="<html><body><p>Final page content.</p></body></html>",
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, "http://93.184.216.34/start")

        assert text is not None
        assert "Final page content" in text
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_stops_after_max_redirects(self, fetcher):
        """Redirect chains that exceed _MAX_REDIRECTS are aborted."""
        hop = 0

        async def infinite_redirect(request):
            nonlocal hop
            hop += 1
            return httpx.Response(
                302,
                headers={"location": f"http://93.184.216.34/hop{hop}"},
            )

        transport = httpx.MockTransport(infinite_redirect)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, "http://93.184.216.34/start")

        assert text is None

    @pytest.mark.asyncio
    async def test_strips_boilerplate_html(self, fetcher):
        html = """<html><body>
            <nav><a href="/">Home</a><a href="/about">About</a></nav>
            <header><h1>Site Header</h1></header>
            <main><p>This is the actual article content.</p></main>
            <aside><p>Sidebar ad content</p></aside>
            <footer><p>Copyright 2025</p></footer>
        </body></html>"""

        async def mock_handler(request):
            return httpx.Response(200, text=html)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, "https://example.com")

        assert text is not None
        assert "actual article content" in text
        assert "Home" not in text
        assert "Site Header" not in text
        assert "Sidebar ad" not in text
        assert "Copyright" not in text

    @pytest.mark.asyncio
    async def test_returns_none_for_non_html(self, fetcher):
        async def pdf_handler(request):
            return httpx.Response(
                200,
                content=b"%PDF-1.4",
                headers={"content-type": "application/pdf"},
            )

        transport = httpx.MockTransport(pdf_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            text = await fetcher._fetch_single(client, "https://example.com/file.pdf")

        assert text is None


class TestTruncation:
    def test_truncates_long_content(self, fetcher):
        long_text = "word " * 2000
        result = fetcher._truncate(long_text)
        assert len(result) < len(long_text)

    def test_preserves_short_content(self, fetcher):
        short_text = "A brief sentence."
        result = fetcher._truncate(short_text)
        assert result == short_text


class TestEnrichResults:
    @pytest.mark.asyncio
    async def test_enriches_only_top_n_results(self, fetcher):
        html = "<html><body><p>Page content for testing.</p></body></html>"

        async def mock_handler(request):
            return httpx.Response(200, text=html)

        transport = httpx.MockTransport(mock_handler)
        results = [_make_result(f"https://example.com/{i}") for i in range(5)]

        async with httpx.AsyncClient(transport=transport) as client:
            fetcher._client_override = client
            enriched = await fetcher.enrich(results)

        for r in enriched[:3]:
            assert r.content is not None
        for r in enriched[3:]:
            assert r.content is None

    @pytest.mark.asyncio
    async def test_failed_fetch_keeps_none_content(self, fetcher):
        async def error_handler(request):
            return httpx.Response(500, text="error")

        transport = httpx.MockTransport(error_handler)
        results = [_make_result()]

        async with httpx.AsyncClient(transport=transport) as client:
            fetcher._client_override = client
            enriched = await fetcher.enrich(results)

        assert enriched[0].content is None


# ---------------------------------------------------------------------------
# fetch_verify_ssl default — regression for #382
# ---------------------------------------------------------------------------


def test_fetch_verify_ssl_default_is_true():
    """TLS verification must be enabled by default for any web-search fetch.

    ``fetch_verify_ssl`` defaults to ``True`` so server-side fetches of
    web-search results verify TLS unless an operator opts out.
    """
    from core.config.retrieval import StaanWebSearchConfig

    cfg = StaanWebSearchConfig()
    assert cfg.fetch_verify_ssl is True


def test_fetch_verify_ssl_is_true_in_the_shipped_yaml():
    """The *effective* value must be True, not just the Pydantic default.

    Regression for #714. The test above constructs the model directly, so it
    passes on the declared default and never sees ``conf/config.yaml``. Since
    the loader merges YAML over that default, `conf/config.yaml` shipping
    ``fetch_verify_ssl: false`` silently disabled TLS verification at runtime
    while this file stayed green — the #404 fix never took effect.

    Assert the shipped YAML directly so a config-level override cannot
    re-disable it without turning a test red.
    """
    import yaml

    conf = yaml.safe_load((Path(__file__).parents[4] / "conf" / "config.yaml").read_text())
    assert conf["websearch"]["fetch_verify_ssl"] is True


# ---------------------------------------------------------------------------
# _guard_request — DNS-resolution-time SSRF guard + verify_ssl default
# ---------------------------------------------------------------------------


class TestGuardRequest:
    @pytest.mark.asyncio
    async def test_blocks_host_resolving_to_private_ip(self, fetcher, monkeypatch):
        # A public-looking hostname that resolves to an internal IP must be blocked.
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

        monkeypatch.setattr("services.websearch.content_fetcher.socket.getaddrinfo", fake_getaddrinfo)
        req = httpx.Request("GET", "http://internal.example.com/")
        with pytest.raises(httpx.RequestError):
            await fetcher._guard_request(req)

    @pytest.mark.asyncio
    async def test_allows_host_resolving_to_public_ip(self, fetcher, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr("services.websearch.content_fetcher.socket.getaddrinfo", fake_getaddrinfo)
        req = httpx.Request("GET", "http://example.com/")
        await fetcher._guard_request(req)  # must not raise


def test_verify_ssl_defaults_to_true():
    # Fetched content feeds the LLM; default to verifying TLS to prevent MITM.
    assert ContentFetcher().verify_ssl is True
