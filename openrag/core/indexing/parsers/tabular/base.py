"""Shared contracts and rendering helpers for streaming tabular parsers."""

from __future__ import annotations

import html
from abc import ABC, abstractmethod
from collections.abc import Iterator

from core.indexing.parsers.document_parser import DocumentParser
from core.models.document import Document, TextBlock


class TabularBatchParser(DocumentParser, ABC):
    """A parser that yields independently indexable table batches."""

    tabular_format: str

    @abstractmethod
    def iter_batches(self, document: Document) -> Iterator[TextBlock]:
        """Yield bounded output batches; backend memory behavior is parser-specific."""


def normalize_headers(headers: list[str]) -> list[str]:
    """Give blank headers stable, collision-free names."""
    used_names = {header for header in headers if header}
    normalized: list[str] = []
    for index, header in enumerate(headers, start=1):
        if header:
            normalized.append(header)
            continue
        base_name = f"column_{index}"
        generated_name = base_name
        suffix = 2
        while generated_name in used_names:
            generated_name = f"{base_name}_{suffix}"
            suffix += 1
        normalized.append(generated_name)
        used_names.add(generated_name)
    return normalized


def markdown_row(cells: list[str]) -> str:
    """Render one safe Markdown table row."""
    escaped = []
    for cell in cells:
        cell = html.escape(cell, quote=False)
        cell = cell.replace("|", "&#124;")
        cell = cell.replace("\r\n", "\n").replace("\r", "\n")
        escaped.append(cell.replace("\n", "<br>"))
    return "| " + " | ".join(escaped) + " |"


def _rendered_header_key(value: str) -> str:
    """Return the canonical cell text produced by the Markdown renderer."""
    return markdown_row([value])[2:-2]


def normalize_unique_headers(values: list[str]) -> list[str]:
    """Normalize headers and retain unique Markdown-rendered identities."""
    headers = normalize_headers([value.strip() for value in values])
    reserved = {_rendered_header_key(name) for name in headers}
    used = set()
    for index, name in enumerate(headers):
        candidate, suffix = name, 2
        candidate_key = _rendered_header_key(candidate)
        if candidate_key in used:
            candidate = f"{name}_{suffix}"
            candidate_key = _rendered_header_key(candidate)
            while candidate_key in reserved or candidate_key in used:
                suffix += 1
                candidate = f"{name}_{suffix}"
                candidate_key = _rendered_header_key(candidate)
        headers[index] = candidate
        used.add(candidate_key)
    return headers
