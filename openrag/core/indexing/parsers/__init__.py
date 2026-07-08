"""DocumentParser ABC + registry."""

from .document_parser import DocumentParser
from .registry import parser_registry

__all__ = ["DocumentParser", "parser_registry"]
