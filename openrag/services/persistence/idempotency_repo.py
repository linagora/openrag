"""Stub :class:`IdempotencyRepository`.

Idempotency support is a post-refactoring P3 feature: cache the
(method, path, body-hash) of a request so retries from a flaky client
return the original response instead of double-applying. When that
lands the implementation is a tiny table keyed by a SHA-256 hash with
a TTL on cleanup; no support exists today.
"""

from __future__ import annotations

from core.ports.idempotency_repo import IdempotencyRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgIdempotencyRepository(_StubRepositoryBase, IdempotencyRepository):
    """TODO: real impl once the ``idempotency_keys`` table is added."""

    async def get_by_hash(self, key_hash: str) -> dict | None:
        raise stub_not_implemented("Idempotency keys")

    async def store(
        self,
        key_hash: str,
        http_method: str,
        status_code: int,
        response_body: bytes,
    ) -> None:
        raise stub_not_implemented("Idempotency keys")


__all__ = ["PgIdempotencyRepository"]
