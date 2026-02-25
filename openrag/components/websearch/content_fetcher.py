import asyncio
import ipaddress
from urllib.parse import urlparse

import httpx
import lxml.html
from components.websearch.base import WebResult
from html_to_markdown import convert
from utils.logger import get_logger

logger = get_logger()

# Rough chars-per-token estimate for truncation (conservative)
_CHARS_PER_TOKEN = 4

# HTML tags that typically contain boilerplate, not main content
_BOILERPLATE_TAGS = {"nav", "footer", "header", "aside", "script", "style", "noscript"}

_USER_AGENT = "Mozilla/5.0 (compatible; OpenRAG/1.0; +https://github.com/linagora/openrag)"


class ContentFetcher:
    """Fetch and extract text content from web search result URLs."""

    def __init__(
        self,
        max_results: int = 3,
        timeout: float = 1.0,
        max_tokens_per_page: int = 500,
        verify_ssl: bool = False,
    ):
        self.max_results = max_results
        self.timeout = timeout
        self.max_tokens_per_page = max_tokens_per_page
        self.verify_ssl = verify_ssl
        self._client_override: httpx.AsyncClient | None = None  # For testing

    def _truncate(self, text: str) -> str:
        """Truncate text to approximately max_tokens_per_page tokens."""
        max_chars = self.max_tokens_per_page * _CHARS_PER_TOKEN
        if len(text) <= max_chars:
            return text
        truncated = text[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > max_chars * 0.8:
            truncated = truncated[:last_space]
        return truncated.rstrip() + " [...]"

    @staticmethod
    def _is_loopback_url(url: str) -> bool:
        host = urlparse(url).hostname or ""
        if host == "localhost":
            return True
        try:
            return not ipaddress.ip_address(host).is_global
        except ValueError:
            return False  # Regular hostname, let it through

    async def _fetch_single(self, client: httpx.AsyncClient, url: str) -> str | None:
        """Fetch a single URL and extract text. Returns None on any failure."""
        # Guard against SSRF: URLs come from the search provider, but a compromised
        # or misbehaving provider could return loopback addresses targeting internal services.
        if self._is_loopback_url(url):
            logger.warning("Blocked loopback URL in web search results", url=url)
            return None
        try:
            response = await asyncio.wait_for(
                client.get(url, follow_redirects=True),
                timeout=self.timeout,
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return None

            html = response.text
            if not html.strip():
                return None

            # Strip boilerplate elements (nav, footer, etc.) before conversion
            try:
                tree = lxml.html.fromstring(html)
                for tag in _BOILERPLATE_TAGS:
                    for el in tree.iter(tag):
                        el.getparent().remove(el)
                html = lxml.html.tostring(tree, encoding="unicode")
            except Exception:
                pass  # If lxml parsing fails, convert the raw HTML

            text = convert(html)
            text = text.strip()
            if not text:
                return None

            return self._truncate(text)

        except TimeoutError:
            logger.debug("Content fetch timed out", url=url)
            return None
        except Exception as e:
            logger.debug("Content fetch failed", url=url, error=str(e))
            return None

    async def enrich(self, results: list[WebResult]) -> list[WebResult]:
        """Fetch content for the top N results in parallel. Mutates results in place."""
        if not results:
            return results

        to_fetch = results[: self.max_results]

        client = self._client_override
        if client is not None:
            tasks = [self._fetch_single(client, r.url) for r in to_fetch]
            contents = await asyncio.gather(*tasks)
            for result, content in zip(to_fetch, contents):
                result.content = content
        else:
            timeout = httpx.Timeout(
                connect=self.timeout,
                read=self.timeout,
                write=self.timeout,
                pool=self.timeout,
            )
            async with httpx.AsyncClient(
                timeout=timeout, verify=self.verify_ssl, headers={"User-Agent": _USER_AGENT}
            ) as client:
                tasks = [self._fetch_single(client, r.url) for r in to_fetch]
                contents = await asyncio.gather(*tasks)
                for result, content in zip(to_fetch, contents):
                    result.content = content

        n_enriched = sum(1 for c in contents if c is not None)
        logger.debug("Content fetching done", enriched=n_enriched, total=len(to_fetch))
        return results
