import asyncio

import httpx
import pytest
from components.websearch.base import WebResult
from components.websearch.content_fetcher import ContentFetcher


@pytest.fixture
def fetcher():
    return ContentFetcher(max_results=3, timeout=1.0, max_tokens_per_page=500)


def _make_result(url="https://example.com", snippet="short snippet"):
    return WebResult(title="Test", url=url, snippet=snippet)


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
