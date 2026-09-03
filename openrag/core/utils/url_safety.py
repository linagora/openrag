"""SSRF guard for server-side URL fetches.

Pure-stdlib host/address checks shared by any server-side fetcher (web-search
content fetch, MCP ``index_url``). Blocks loopback / private / link-local /
reserved / non-global addresses and the alternate IPv4 encodings that
resolvers accept (decimal-integer, hex, octal, short form). Regular hostnames
pass the literal check; callers that follow redirects MUST re-validate every
hop, since a public hostname can redirect to a private target.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _bare_numeric_host_value(host: str) -> int | None:
    """Parse *host* as a single C-style integer literal (decimal, ``0x`` hex,
    or ``0``-prefixed octal) — the form ``inet_aton`` accepts when a host has
    no dots. Returns ``None`` if *host* contains a dot or isn't purely such a
    literal.
    """
    if "." in host:
        return None
    try:
        if host.lower().startswith("0x"):
            return int(host, 16)
        if len(host) > 1 and host[0] == "0" and host.isdigit():
            return int(host, 8)
        if host.isdigit():
            return int(host, 10)
    except ValueError:
        return None
    return None


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


def is_safe_url(url: str, *, allow_private_hosts: bool = False) -> bool:
    """Return True only if *url* is safe for a server-side fetch.

    Blocks non-HTTP(S) schemes, ``localhost``, IPv4/IPv6 literals in
    private/loopback/link-local/reserved ranges, and the alternate IPv4
    spellings a resolver expands (``2130706433``, ``0x7f000001``,
    ``0177.0.0.1``, ``127.1`` — all ``127.0.0.1``). Regular hostnames pass —
    the caller must re-check each redirect hop.

    ``allow_private_hosts`` keeps the scheme check but skips the address checks.
    Never enable it for a fetch target an end user controls.
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

    if allow_private_hosts:
        return True

    if host.lower() == "localhost":
        return False

    # Dotted-decimal or IPv6 literal (e.g. "127.0.0.1", "::1", "10.0.0.1").
    try:
        return not is_blocked_address(ipaddress.ip_address(host))
    except ValueError:
        pass

    # A bare (undotted) numeric host that doesn't fit in 32 bits: block it
    # ourselves rather than trust inet_aton's overflow handling, which is not
    # portable — glibc rejects such a host outright (matching the real
    # resolver, confirmed via getaddrinfo), but at least one other libc has
    # been observed to silently wrap it mod 2**32 into a blocked address
    # instead of raising. Relying on ip_address(int(host)) here would have
    # the opposite problem: it never raises for a value this large, it just
    # silently builds an unrelated, often-public-looking IPv6 address.
    numeric_value = _bare_numeric_host_value(host)
    if numeric_value is not None and numeric_value > 0xFFFFFFFF:
        return False

    # inet_aton covers decimal/hex/octal/short-form IPv4 the same way glibc's
    # resolver does, for every in-range value.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        pass
    else:
        return not is_blocked_address(ipaddress.IPv4Address(packed))

    # Regular hostname — passes the literal check; redirect hops re-validated.
    return True


__all__ = ["is_blocked_address", "is_safe_url"]
