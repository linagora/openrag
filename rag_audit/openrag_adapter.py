from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from rag_audit.models import AuditChunk, AuditDocument


def from_openrag_documents(
    openrag_chunks: list[Any],
    file_records: list[dict[str, Any]],
) -> tuple[list[AuditDocument], list[AuditChunk]]:
    file_metadata = _file_metadata_by_id(file_records)
    documents = [_audit_document(file_id, metadata) for file_id, metadata in sorted(file_metadata.items())]

    grouped_chunks: dict[str, list[Any]] = {}
    for chunk in openrag_chunks:
        metadata = _metadata(chunk)
        file_id = str(metadata.get("file_id") or metadata.get("document_id") or "unknown")
        grouped_chunks.setdefault(file_id, []).append(chunk)
        if file_id not in file_metadata:
            file_metadata[file_id] = metadata

    known_docs = {doc.id for doc in documents}
    for file_id, metadata in sorted(file_metadata.items()):
        if file_id not in known_docs:
            documents.append(_audit_document(file_id, metadata))

    audit_chunks: list[AuditChunk] = []
    for file_id, chunks in sorted(grouped_chunks.items()):
        ordered = sorted(chunks, key=_chunk_sort_key)
        for index, chunk in enumerate(ordered):
            metadata = dict(_metadata(chunk))
            content = str(getattr(chunk, "page_content", "") or "")
            chunk_id = str(metadata.get("_id") or metadata.get("section_id") or f"{file_id}#{index}")
            audit_chunks.append(
                AuditChunk(
                    id=chunk_id,
                    document_id=file_id,
                    content=content,
                    content_hash=_hash(content),
                    token_count=len(content.split()),
                    chunk_index=index,
                    heading_path=str(metadata.get("heading_path") or _first_heading(content)),
                    metadata=metadata,
                )
            )

    return documents, audit_chunks


def _file_metadata_by_id(file_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in file_records:
        file_id = record.get("file_id")
        if file_id is None:
            continue
        indexed[str(file_id)] = dict(record)
    return indexed


def _audit_document(file_id: str, metadata: dict[str, Any]) -> AuditDocument:
    filename = metadata.get("original_filename") or metadata.get("filename") or file_id
    source = str(metadata.get("source") or "")
    doc_type = str(metadata.get("doc_type") or _doc_type(filename, metadata.get("mimetype")))
    created_at = _parse_datetime(metadata.get("created_at") or metadata.get("indexed_at"))
    source_modified_at = _parse_datetime(metadata.get("source_modified_at"))
    content_identity = "|".join(str(metadata.get(k, "")) for k in ("file_id", "source", "filename", "created_at"))
    return AuditDocument(
        id=str(file_id),
        title=str(filename),
        content_hash=_hash(content_identity),
        author=str(metadata.get("author") or ""),
        source_modified_at=source_modified_at,
        doc_type=doc_type,
        path=source,
        source_url=str(metadata.get("source_url") or ""),
        source_name=str(metadata.get("partition") or metadata.get("source_name") or "openrag"),
        created_at=created_at,
        metadata=dict(metadata),
    )


def _metadata(chunk: Any) -> dict[str, Any]:
    raw = getattr(chunk, "metadata", None)
    return dict(raw or {})


def _chunk_sort_key(chunk: Any) -> tuple[int, int]:
    metadata = _metadata(chunk)
    section_id = _as_int(metadata.get("section_id"))
    milvus_id = _as_int(metadata.get("_id"))
    page = _as_int(metadata.get("page"))
    return (section_id if section_id is not None else page or 0, milvus_id or 0)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _doc_type(filename: Any, mimetype: Any) -> str:
    if mimetype:
        return str(mimetype).split("/")[-1]
    suffix = Path(str(filename)).suffix
    return suffix.lstrip(".") if suffix else ""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _first_heading(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    match = re.search(r"\* filename:\s*(.+)", content, re.I)
    return match.group(1).strip() if match else ""


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
