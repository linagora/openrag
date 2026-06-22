"""Port for the document-serialization operation (file → raw text).

``ConversionService`` exposes the ``extractText`` tool / ``/extract`` route —
serialize an uploaded file to raw text. Defining the operation on a dedicated
port keeps the orchestrator decoupled from the parser/Ray infrastructure
(no Ray import / remote call under ``services/orchestrators/``); the
composition root injects the concrete implementation
(``services/workers/parsers/file_serializer.py::ParserFileSerializer``, which
runs the parser dispatcher in-process).

No Ray / LangChain types leak across this boundary — the serialized document
is returned as its plain text content.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class FileSerializer(ABC):
    """The single serialize operation the conversion orchestrator needs."""

    @abstractmethod
    async def serialize(self, path: str, metadata: dict) -> str:
        """Serialize the file at ``path`` and return its raw text content."""
        ...
