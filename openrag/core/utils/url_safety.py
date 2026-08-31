"""SSRF guard for server-side URL fetches.

Pure-stdlib host/address checks shared by any server-side fetcher (web-search
content fetch, MCP ``index_url``). Blocks loopback / private / link-local /
reserved / non-global addresses and the decimal-integer IPv4 encoding that
resolvers accept. Regular hostnames pass the literal check; callers that follow
redirects MUST re-validate every hop, since a public hostname can redirect to a
private target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
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


def is_safe_url(url: str, *, allow_private_hosts: bool = False) -> bool:
    """Return True only if *url* is safe for a server-side fetch.

    Blocks non-HTTP(S) schemes, ``localhost``, IPv4/IPv6 literals in
    private/loopback/link-local/reserved ranges, and decimal-integer-encoded
    IPv4 (e.g. ``2130706433`` == ``127.0.0.1``). Regular hostnames pass — the
    caller must re-check each redirect hop.

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

    # Decimal-integer form (e.g. 2130706433 → 127.0.0.1). ip_address(int)
    # interprets the value as a packed IPv4 address, matching glibc's resolver.
    try:
        return not is_blocked_address(ipaddress.ip_address(int(host)))
    except (ValueError, TypeError):
        pass

    # Regular hostname — passes the literal check; redirect hops re-validated.
    return True


async def resolves_to_public_addresses(scheme: str, host: str, port: int | None) -> bool:
    """True only if every A/AAAA record for *host* is an address we may contact.

    ``is_safe_url`` inspects the literal host, so a public-looking name pointing
    at 169.254.169.254 passes it.

    Not a rebinding fix: the record can change before the connect, and closing
    that needs the socket pinned to the validated address, which httpx does not
    expose. Accepted here — the body is fixed and its response never reaches
    the caller.
    """
    default_port = 443 if scheme == "https" else 80
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port or default_port, proto=socket.IPPROTO_TCP)
    except Exception:
        # Unresolvable — the request could not have succeeded anyway.
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            # Strip any IPv6 scope id ("fe80::1%eth0") before parsing.
            parsed_address = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        if is_blocked_address(parsed_address):
            return False
    return True


__all__ = ["is_blocked_address", "is_safe_url", "resolves_to_public_addresses"]
