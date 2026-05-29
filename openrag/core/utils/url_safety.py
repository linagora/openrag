"""SSRF guard for server-side URL fetches.

Pure-stdlib host/address checks shared by any server-side fetcher (web-search
content fetch, MCP ``index_url``). Blocks loopback / private / link-local /
reserved / non-global addresses and the decimal-integer IPv4 encoding that
resolvers accept. Regular hostnames pass the literal check; callers that follow
redirects MUST re-validate every hop, since a public hostname can redirect to a
private target.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any IP a server-side fetcher must not contact.

    Checks every private/reserved/non-global flag explicitly so the guard is
    correct across Python minor releases (``is_global`` semantics shifted
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


def is_safe_url(url: str) -> bool:
    """Return True only if *url* is safe for a server-side fetch.

    Blocks non-HTTP(S) schemes, ``localhost``, IPv4/IPv6 literals in
    private/loopback/link-local/reserved ranges, and decimal-integer-encoded
    IPv4 (e.g. ``2130706433`` == ``127.0.0.1``). Regular hostnames pass — the
    caller must re-check each redirect hop.
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

    # Dotted-decimal or IPv6 literal (e.g. "127.0.0.1", "::1", "10.0.0.1").
    try:
        return not is_blocked_address(ipaddress.ip_address(host))
    except ValueError:
        pass

    # Decimal-integer form (e.g. 2130706433 → 127.0.0.1). ip_address(int)
    # interprets the value as a packed IPv4 address, matching glibc's resolver.
    try:
        return not is_blocked_address(ipaddress.ip_address(int(host)))
    except (ValueError, TypeError):
        pass

    # Regular hostname — passes the literal check; redirect hops re-validated.
    return True


__all__ = ["is_blocked_address", "is_safe_url"]
