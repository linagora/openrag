"""DocumentParser ABC + registry."""

from openrag.core.indexing.parsers.document_parser import DocumentParser
from openrag.core.indexing.parsers.registry import parser_registry

__all__ = ["DocumentParser", "parser_registry"]
