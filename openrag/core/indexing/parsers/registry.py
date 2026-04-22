"""Document parser registry."""

from openrag.core.utils.registry import Registry

from .document_parser import DocumentParser

parser_registry: Registry[DocumentParser] = Registry("parser")
