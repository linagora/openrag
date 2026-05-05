"""Abstract document parser interface and category markers.

``DocumentParser`` is the single port every concrete parser implements.

Two empty subclasses are exposed alongside it as **type markers** —
they categorize a parser by *how* it gets its work done, without adding
any behaviour:

- ``BasePooledParser`` — a parser whose ``parse()`` is satisfied by a
  pool of workers (Ray actors, ProcessPoolExecutor, asyncio task group,
  …). Concrete impls live in ``services/``.
- ``BaseClientParser`` — a parser whose ``parse()`` is satisfied by an
  external client (HTTP service, gRPC, vendor SDK, …). Concrete impls
  live in ``services/``.

The markers exist so consumers (e.g. ``MarkerParser(pool: BasePooledParser)``)
can constrain the *kind* of parser they accept tighter than the
generic ``DocumentParser``. Concrete subclasses implement
``parse()`` and ``supported_types()`` directly — no extra hook method.

If a shared pattern (retry, timeout, semaphore, …) ever materialises
across multiple subclasses, lift it into the base then.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ...models.document import Document, ProcessedDocument


class DocumentParser(ABC):
    """Base class for all document parsers (PDF, text, HTML, image, audio, etc.)."""

    @abstractmethod
    async def parse(self, document: Document) -> ProcessedDocument:
        """Parse a document into text blocks and images."""
        ...

    @abstractmethod
    def supported_types(self) -> list[str]:
        """Return list of DocumentType values this parser handles."""
        ...


class BasePooledParser(DocumentParser, ABC):
    """Marker for parsers backed by a worker pool. Concrete impl in services/."""


class BaseClientParser(DocumentParser, ABC):
    """Marker for parsers backed by an external client. Concrete impl in services/."""
