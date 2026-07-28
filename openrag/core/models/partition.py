"""Partition naming rules — which names exist, and which are spoken for.

Kept out of any one feature's module because the callers are generic:
``PartitionService`` decides what may be created and what a listing shows,
``RetrievalService`` decides what ``all`` expands to. None of them should have
to name the subsystem that owns a namespace in order to ask the question.

Adding an internal namespace is a line in ``INTERNAL_PARTITION_PREFIXES``. The
call sites do not change.
"""

from __future__ import annotations

from .evaluation import EVAL_PARTITION_PREFIX

#: Names that collide with a cross-partition sentinel (``openrag-all``,
#: ``?partitions=all``). A real partition named ``all`` would make the admin
#: partition-list route expand to *every* partition. Never creatable, by any
#: caller — no internal caller has a reason to want it either.
RESERVED_PARTITION_NAMES = frozenset({"all"})

#: Prefixes owned by an internal subsystem. A partition under one of these is
#: real but not user-facing: hidden from the listings and from the ``all``
#: fan-out, and creatable only by the subsystem that owns the namespace.
#: ``__eval_`` belongs to an evaluation run — see ``core.models.evaluation``.
INTERNAL_PARTITION_PREFIXES = (EVAL_PARTITION_PREFIX,)


def is_internal_partition(partition: str) -> bool:
    """True for a partition in a namespace an internal subsystem owns.

    Hiding one of these from a listing is only half the rule: see
    ``is_reserved_partition_name`` for why it must also be unwritable.
    """
    return partition.startswith(INTERNAL_PARTITION_PREFIXES)


def is_reserved_partition_name(partition: str, *, allow_internal: bool = False) -> bool:
    """True if ``partition`` may not be created by this caller.

    Compared on the stripped, lowercased name, so neither ``"  all  "`` nor
    ``__EVAL_x`` can be spelled to sit just outside the check — the latter
    would otherwise be a real, quota-consuming partition that no listing, and
    therefore no admin audit, can see.

    Reserving the internal prefixes is also what keeps the wildcard honest.
    They are hidden from the listings and from ``RetrievalService``'s ``all``
    fan-out, but *not* from a SUPER_ADMIN_MODE admin's raw
    ``GET /search?partitions=all``, which is an intentionally unscoped Milvus
    query with no partition clause to narrow. Making the names unwritable is
    what guarantees the only rows there are a live subsystem's own.

    ``allow_internal`` lets a subsystem claim its own namespace; it is never
    reachable from the HTTP surface. It does not unlock
    ``RESERVED_PARTITION_NAMES``.
    """
    normalized = partition.strip().lower()
    if normalized in RESERVED_PARTITION_NAMES:
        return True
    return is_internal_partition(normalized) and not allow_internal


__all__ = [
    "INTERNAL_PARTITION_PREFIXES",
    "RESERVED_PARTITION_NAMES",
    "is_internal_partition",
    "is_reserved_partition_name",
]
