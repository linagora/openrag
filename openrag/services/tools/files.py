"""The agent's tools over the catalog: find files by name, date and type, and describe one.

``find_files`` looks the files up in the catalog; it doesn't read them. It
matches the file names (``core.tools.file_names``) and filters on the
document's date, the upload date and the type. What files *say* is
``search_knowledge``'s job. Both tools stay within the request's scope.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from core.models.catalog import CatalogFile
from core.tools import ToolContext, ToolError, tool
from core.tools.file_names import rank_by_name
from core.tools.passages import one_line
from core.utils.consts import UPLOAD_METADATA_SERVER_KEYS, is_internal_metadata_key
from core.utils.logging import get_logger
from pydantic import Field
from services.tools.documents import CreatedAfter, CreatedBefore

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository
    from core.ports.topic_tag_repo import TopicTagRepository

logger = get_logger()

#: Files of a scope a lookup reads, the most recently indexed first.
CANDIDATE_LIMIT = 20_000

_KINDS = {"image": "image/", "audio": "audio/", "video": "video/"}
#: Metadata ``get_file_info`` shows on its own lines, or never (the server's own keys, such as the file's path).
_NOT_OTHER_METADATA = UPLOAD_METADATA_SERVER_KEYS | {
    "mimetype",
    "created_at",
    "partition",
    "indexed_at",
    "degraded_stages",
}
_MAX_OTHER_METADATA = 10
_MAX_TOPICS = 20

_FIND_FILES = (
    "Find files by their name, dates or type among the files this conversation can see. Give words from the "
    "file name, not a sentence. Lists files, not their content: search_file searches one, get_file_info "
    "describes one, and search_knowledge finds files by what they say."
)
_GET_FILE_INFO = "Describe one file: its names, type, size, dates, number of passages and topics."

UploadedAfter = Annotated[
    date | None, Field(description="Only files uploaded on or after this date (YYYY-MM-DD, UTC).")
]
UploadedBefore = Annotated[
    date | None, Field(description="Only files uploaded before this date, which is excluded (YYYY-MM-DD, UTC).")
]
SortBy = Literal["match", "created", "uploaded"]


class FileTools:
    """``find_files`` and ``get_file_info``."""

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        topic_tags: TopicTagRepository | None = None,
        candidate_limit: int = CANDIDATE_LIMIT,
    ) -> None:
        self._repo = document_repo
        self._topic_tags = topic_tags
        self._candidate_limit = candidate_limit

    @tool("find_files", _FIND_FILES)
    async def find_files(
        self,
        ctx: ToolContext,
        name: Annotated[
            str | None, Field(max_length=300, description='Words from the file name, e.g. "contrat acme 2024".')
        ] = None,
        file_types: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=20)]] | None,
            Field(max_length=5, description='Extensions such as "pdf", "docx", "xlsx", or "image", "audio", "video".'),
        ] = None,
        created_after: CreatedAfter = None,
        created_before: CreatedBefore = None,
        uploaded_after: UploadedAfter = None,
        uploaded_before: UploadedBefore = None,
        sort_by: Annotated[
            SortBy | None,
            Field(description="By name match (the default with a name), document date, or upload date (otherwise)."),
        ] = None,
        oldest_first: Annotated[bool, Field(description="Oldest first when sorting by a date.")] = False,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> str:
        partitions = ctx.scope.partitions
        if not partitions:
            raise ToolError("This conversation has no documents.")
        for after, before, which in (
            (created_after, created_before, "created"),
            (uploaded_after, uploaded_before, "uploaded"),
        ):
            if after and before and after >= before:
                raise ToolError(f"{which}_after must be earlier than {which}_before.")

        files = await self._repo.list_catalog_files(
            None if "all" in partitions else partitions,
            file_ids=ctx.scope.file_ids,
            limit=self._candidate_limit + 1,
        )
        truncated = len(files) > self._candidate_limit
        name = (name or "").strip()
        sort = sort_by if sort_by and (sort_by != "match" or name) else ("match" if name else "uploaded")
        # Up to CANDIDATE_LIMIT files to filter and match: CPU work, kept off
        # the event loop so the other requests of this process aren't held up.
        found, unmatched = await asyncio.to_thread(
            _select,
            files[: self._candidate_limit],
            name=name,
            file_types=file_types,
            created=(created_after, created_before),
            uploaded=(uploaded_after, uploaded_before),
            sort=sort,
            oldest_first=oldest_first,
        )

        notes = []
        if unmatched:
            notes.append(f"No file name has: {', '.join(unmatched)}.")
        if truncated:
            notes.append(f"Only the {self._candidate_limit} most recently uploaded files were looked at.")
        if not found:
            hint = "Try fewer words, other filters, or search_knowledge to find files by what they say."
            return " ".join(["No file matched.", *notes, hint])
        order = {"match": "best name match first", "created": "by document date", "uploaded": "by upload date"}[sort]
        if sort != "match":
            order += ", oldest first" if oldest_first else ", newest first"
        lines = [f"{min(limit, len(found))} of {len(found)} files, {order}.", *notes]
        for number, f in enumerate(found[:limit], 1):
            lines.append(f"{number}. {one_line(_display_name(f))}")
            lines.append(f"   {_details(f)}")
        return "\n".join(lines)

    @tool("get_file_info", _GET_FILE_INFO)
    async def get_file_info(
        self,
        ctx: ToolContext,
        partition: Annotated[str, Field(description="The partition shown with the file.")],
        file_id: Annotated[str, Field(description="The file id shown with the file.")],
    ) -> str:
        if not ctx.allows_file(partition, file_id):
            raise ToolError(f"File '{file_id}' of partition '{partition}' is not among this conversation's documents.")
        found = await self._repo.list_catalog_files([partition], file_ids=[file_id], limit=1)
        if not found:
            raise ToolError(f"There is no file '{file_id}' in partition '{partition}'.")
        f = found[0]
        metadata = await self._repo.get_file_metadata(file_id, partition) or {}

        lines = [f"File: {one_line(_display_name(f))}"]
        if f.filename and f.original_filename and f.filename != f.original_filename:
            lines.append(f"Stored as: {one_line(f.filename)}")
        lines.append(_details(f))
        topics = await self._topics(file_id, partition)
        if topics:
            lines.append("Topics: " + ", ".join(one_line(topic) for topic in topics[:_MAX_TOPICS]))
        degraded = metadata.get("degraded_stages")
        if isinstance(degraded, list) and degraded:
            stages = ", ".join(one_line(stage) for stage in degraded)
            lines.append(f"Indexing steps that failed: {stages}. Some of its content may be missing.")
        other = [
            (key, value)
            for key, value in metadata.items()
            if key not in _NOT_OTHER_METADATA
            and not is_internal_metadata_key(key)
            and isinstance(value, str | int | float | bool)
            and str(value).strip()
        ][:_MAX_OTHER_METADATA]
        if other:
            lines.append("Other metadata:")
            lines.extend(f"- {one_line(key)}: {one_line(value)}" for key, value in other)
        return "\n".join(lines)

    async def _topics(self, file_id: str, partition: str) -> list[str]:
        """The file's topic tags; none when tagging is off, or when they can't be read (logged)."""
        if self._topic_tags is None:
            return []
        try:
            rows = await self._topic_tags.get_by_document(file_id, partition)
        except Exception:
            logger.bind(partition=partition, file_id=file_id).exception("Could not read a file's topic tags")
            return []
        return [row["tag"] for row in rows if row.get("tag")]


def _select(
    files: list[CatalogFile],
    *,
    name: str,
    file_types: list[str] | None,
    created: tuple[date | None, date | None],
    uploaded: tuple[date | None, date | None],
    sort: SortBy,
    oldest_first: bool,
) -> tuple[list[CatalogFile], list[str]]:
    """The *files* that pass the filters and match *name*, in *sort* order, and the name's words no file has."""
    files = [
        f
        for f in files
        if _within(_created(f), *created, required=any(created))
        and _within(_uploaded(f), *uploaded, required=False)
        and (not file_types or _has_type(f, file_types))
    ]
    unmatched: list[str] = []
    if not name:
        found = files
    elif exact := [f for f in files if f.file_id == name]:
        found = exact
    else:
        ranking = rank_by_name(name, [_names(f) for f in files])
        found = [files[m.index] for m in ranking.matches]
        unmatched = ranking.unmatched
    if sort == "created":
        dated = sorted((f for f in found if _created(f)), key=_created, reverse=not oldest_first)
        found = dated + [f for f in found if not _created(f)]
    elif sort == "uploaded":
        found = sorted(found, key=_uploaded, reverse=not oldest_first)
    return found, unmatched


def _names(f: CatalogFile) -> list[str]:
    return list(dict.fromkeys(n for n in (f.original_filename, f.filename) if n))


def _display_name(f: CatalogFile) -> str:
    return f.original_filename or f.filename or f.file_id


def _details(f: CatalogFile) -> str:
    created = _created(f)
    return " | ".join(
        [
            f"Partition: {one_line(f.partition)}",
            f"File id: {one_line(f.file_id)}",
            f"Type: {one_line(f.mimetype or 'unknown')}",
            f"Size: {one_line(f.file_size or 'unknown')}",
            f"Passages: {f.chunk_count if f.chunk_count is not None else 'unknown'}",
            f"Created: {created.isoformat() if created else 'unknown'}",
            f"Uploaded: {_uploaded(f).isoformat()}",
        ]
    )


def _created(f: CatalogFile) -> date | None:
    """The document's own date, in UTC; ``None`` when the client sent none, or one that isn't a date."""
    if not f.created_at:
        return None
    try:
        parsed = datetime.fromisoformat(f.created_at.strip())
    except ValueError:
        return None
    return (parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)).date()


def _uploaded(f: CatalogFile) -> date:
    indexed_at = f.indexed_at
    return (indexed_at.astimezone(UTC) if indexed_at.tzinfo else indexed_at.replace(tzinfo=UTC)).date()


def _within(day: date | None, after: date | None, before: date | None, *, required: bool) -> bool:
    """Whether *day* is in ``[after, before)``; a missing *day* passes only when no bound is *required*."""
    if day is None:
        return not required
    return (after is None or day >= after) and (before is None or day < before)


def _has_type(f: CatalogFile, file_types: list[str]) -> bool:
    mimetype = (f.mimetype or "").lower()
    extensions = {name.rsplit(".", 1)[-1].lower() for name in _names(f) if "." in name}
    for wanted in (t.lower().lstrip(".") for t in file_types):
        if wanted in _KINDS and mimetype.startswith(_KINDS[wanted]):
            return True
        if wanted in extensions or mimetype.endswith(f"/{wanted}"):
            return True
    return False


__all__ = ["FileTools"]
