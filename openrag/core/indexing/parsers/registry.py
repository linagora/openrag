"""Document parser registry."""

from openrag.core.indexing.parsers.document_parser import DocumentParser
from openrag.core.utils.registry import Registry

parser_registry: Registry[DocumentParser] = Registry("parser")
