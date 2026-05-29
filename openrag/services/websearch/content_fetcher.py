import asyncio
import ipaddress
from urllib.parse import urljoin, urlparse

import httpx
import lxml.html
from core.utils.logging import get_logger
from html_to_markdown import convert
from services.websearch.base import WebResult

logger = get_logger()

# Rough chars-per-token estimate for truncation (conservative)
_CHARS_PER_TOKEN = 4

# HTML tags that typically contain boilerplate, not main content
_BOILERPLATE_TAGS = {"nav", "footer", "header", "aside", "script", "style", "noscript"}

_USER_AGENT = "Mozilla/5.0 (compatible; OpenRAG/1.0; +https://github.com/linagora/openrag)"

_MAX_REDIRECTS = 10


def _is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any IP that a server-side fetcher must not contact.

    Checks all private/reserved/non-global flags explicitly so the guard is
    correct across Python minor releases (``is_global`` semantics changed
    between 3.10 and 3.11 for CGNAT and some multicast ranges).
    """
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
        or not addr.is_global
    )


def _is_safe_url(url: str) -> bool:
    """Return True only if *url* is safe for a server-side fetch.

    Blocks:
    - Non-HTTP(S) schemes
    - ``localhost`` hostname
    - IPv4/IPv6 literals in private, loopback, link-local, or reserved ranges
    - Decimal-integer-encoded IPv4 addresses (e.g. ``2130706433`` == ``127.0.0.1``)

    Regular hostnames pass through; per-hop redirect validation (in
    :meth:`ContentFetcher._fetch_single`) re-checks every redirect target,
    covering the case where a public hostname redirects to a private address.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.hostname
    if not host:
        return False

    if host.lower() == "localhost":
        return False

    # Dotted-decimal or IPv6 literal (e.g. "127.0.0.1", "::1", "10.0.0.1")
    try:
        return not _is_blocked_address(ipaddress.ip_address(host))
    except ValueError:
        pass

    # Decimal-integer form (e.g. 2130706433 → 127.0.0.1).
    # ipaddress.ip_address(int) interprets the value as a packed IPv4 address,
    # matching how glibc's resolver (and therefore httpx) handles such hostnames.
    try:
        return not _is_blocked_address(ipaddress.ip_address(int(host)))
    except (ValueError, TypeError):
        pass

    # Regular hostname — passes initial check; every redirect hop is re-validated.
    return True


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

    async def _fetch_single(self, client: httpx.AsyncClient, url: str) -> str | None:
        """Fetch a single URL and extract text. Returns None on any failure.

        Redirects are followed manually (``follow_redirects=False``) so every
        hop is validated by :func:`_is_safe_url` before the request is sent.
        This prevents a legitimate initial URL from redirecting to a private
        address after the initial check passes.
        """
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            if not _is_safe_url(current_url):
                logger.warning("Blocked unsafe URL in web search results", url=current_url)
                return None

            try:
                response = await asyncio.wait_for(
                    client.get(current_url, follow_redirects=False),
                    timeout=self.timeout,
                )
            except TimeoutError:
                logger.debug("Content fetch timed out", url=current_url)
                return None
            except Exception as e:
                logger.debug("Content fetch failed", url=current_url, error=str(e))
                return None

            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue

            try:
                response.raise_for_status()
            except Exception as e:
                logger.debug("Content fetch failed", url=current_url, error=str(e))
                return None

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return None

            html = response.text
            if not html.strip():
                return None

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

        logger.debug("Too many redirects, giving up", url=url)
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
