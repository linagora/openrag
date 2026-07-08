"""Stub :class:`AuditLogRepository`.

Audit logging is a post-refactoring P2 feature (enterprise compliance):
an append-only record of "who did what when" against the catalog. When
that lands the implementation is straightforward — one INSERT per
sensitive API call, paginated SELECTs from an admin route — but no
table exists today so every method raises.
"""

from __future__ import annotations

from typing import Any

from core.ports.audit_log_repo import AuditLogRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgAuditLogRepository(_StubRepositoryBase, AuditLogRepository):
    """TODO: real impl once the ``audit_log`` table is added."""

    async def insert(
        self,
        user_id: int | None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details_json: dict | None = None,
        request_id: str | None = None,
    ) -> None:
        raise stub_not_implemented("Audit log")

    async def query(
        self,
        filters: dict[str, Any],
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict]:
        raise stub_not_implemented("Audit log")


__all__ = ["PgAuditLogRepository"]
