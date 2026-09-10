"""Document parser registry."""

from core.utils.registry import Registry

from .document_parser import DocumentParser

parser_registry: Registry[DocumentParser] = Registry("parser")
