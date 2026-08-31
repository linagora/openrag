"""SSRF guard for server-side URL fetches.

Pure-stdlib host/address checks shared by any server-side fetcher (web-search
content fetch, MCP ``index_url``). Blocks loopback / private / link-local /
reserved / non-global addresses and the alternate IPv4 encodings that
resolvers accept (decimal-integer, hex, octal, short form). Regular hostnames
pass the literal check, so a fetcher must also call ``resolve_public_addresses``
and connect to an address it returns. Callers that follow redirects MUST
re-validate every hop, since a public hostname can redirect to a private target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import NamedTuple
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

    # Decimal-integer form (e.g. 2130706433 → 127.0.0.1). ip_address(int)
    # interprets the value as a packed IPv4 address, matching glibc's resolver.
    try:
        return not is_blocked_address(ipaddress.ip_address(int(host)))
    except (ValueError, TypeError):
        pass

    # Spellings ``inet_aton`` accepts but ``ip_address``/``int`` reject: hex
    # ("0x7f000001"), octal ("0177.0.0.1") and short forms ("127.1"). Callers
    # without a resolution step would otherwise treat these as hostnames.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        pass
    else:
        return not is_blocked_address(ipaddress.IPv4Address(packed))

    # Regular hostname — passes the literal check; redirect hops re-validated.
    return True


# Every record is an address we may contact; ``addresses`` is populated.
RESOLUTION_PUBLIC = "public"
# At least one record is off-limits — refuse the fetch.
RESOLUTION_BLOCKED = "blocked"
# The name has no records. A caller error, and permanent.
RESOLUTION_UNKNOWN_HOST = "unknown_host"
# *Our* resolver could not answer (SERVFAIL, timeout). Says nothing about the
# name, so a caller must not be told their URL is bad because of it.
RESOLUTION_RESOLVER_FAILED = "resolver_failed"


class HostResolution(NamedTuple):
    """What resolving a fetch target's host told us."""

    status: str
    addresses: tuple[str, ...] = ()

    @property
    def is_public(self) -> bool:
        return self.status == RESOLUTION_PUBLIC


async def resolve_public_addresses(scheme: str, host: str, port: int | None) -> HostResolution:
    """Resolve *host* and classify what came back.

    ``is_safe_url`` inspects the literal host, so a public-looking name pointing
    at 169.254.169.254 passes it; this is the check that catches that.

    On ``RESOLUTION_PUBLIC`` the validated addresses come back with the result,
    in the resolver's own preference order. Connect to one of them rather than
    re-resolving the name: a record that changes between this lookup and the
    connect would otherwise move the request to an internal host.
    """
    default_port = 443 if scheme == "https" else 80
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port or default_port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        # EAI_AGAIN/EAI_FAIL mean the resolver failed, not that the name is bad.
        transient = exc.errno in (socket.EAI_AGAIN, socket.EAI_FAIL)
        return HostResolution(RESOLUTION_RESOLVER_FAILED if transient else RESOLUTION_UNKNOWN_HOST)
    except Exception:
        return HostResolution(RESOLUTION_RESOLVER_FAILED)

    # Order preserved: getaddrinfo returns the preferred address first (RFC 6724).
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        return HostResolution(RESOLUTION_UNKNOWN_HOST)
    for address in addresses:
        try:
            # Strip any IPv6 scope id ("fe80::1%eth0") before parsing.
            parsed_address = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return HostResolution(RESOLUTION_BLOCKED)
        if is_blocked_address(parsed_address):
            return HostResolution(RESOLUTION_BLOCKED)
    return HostResolution(RESOLUTION_PUBLIC, tuple(addresses))


async def resolves_to_public_addresses(scheme: str, host: str, port: int | None) -> bool:
    """``resolve_public_addresses`` for callers that only need the verdict."""
    return (await resolve_public_addresses(scheme, host, port)).is_public


__all__ = [
    "RESOLUTION_BLOCKED",
    "RESOLUTION_PUBLIC",
    "RESOLUTION_RESOLVER_FAILED",
    "RESOLUTION_UNKNOWN_HOST",
    "HostResolution",
    "is_blocked_address",
    "is_safe_url",
    "resolve_public_addresses",
    "resolves_to_public_addresses",
]
