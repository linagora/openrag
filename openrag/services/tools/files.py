"""The agent's tools over the catalog: find files by name, date, type or subject, and describe one.

- ``find_files`` lists files from the catalog, never their content. It
  matches the file names (``core.tools.file_names``) and filters on the
  document's date, the upload date and the type. With ``about``, it ranks the
  files by what they discuss: one search of their passages, grouped by file,
  each file ranked by its best passage, which it shows as an excerpt numbered
  ``[Source N]`` for ``read_passage`` to open.
- ``get_file_info`` describes one file from its catalog row, its upload
  metadata and its topic tags.

Both tools stay within the request's scope.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from core.chat.routing import Route
from core.models.catalog import CatalogFile
from core.models.query import Query, SearchQueries
from core.tools import ToolContext, ToolError, tool
from core.tools.file_names import rank_by_name
from core.tools.passages import excerpt, one_line
from core.utils.consts import UPLOAD_METADATA_SERVER_KEYS, is_internal_metadata_key
from core.utils.exceptions import PartitionNotFoundError
from core.utils.logging import get_logger
from core.utils.text import neutralize_prompt_control_tokens
from pydantic import Field
from services.tools.documents import CreatedAfter, CreatedBefore, created_between

if TYPE_CHECKING:
    from core.models.chunk import Chunk
    from core.ports.document_repo import DocumentRepository
    from core.ports.topic_tag_repo import TopicTagRepository
    from services.capabilities.retrieve import SourceRetriever

logger = get_logger()

#: Files of a scope a lookup reads, the most recently indexed first.
CANDIDATE_LIMIT = 20_000

#: Passages a search by subject ranks the files by. Not reranked: the
#: reranker keeps its own top few, which one long file can fill.
SUBJECT_PASSAGES = 100

#: Characters of a file's best passage shown under it.
BEST_PASSAGE_CHARS = 200

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
    "Find files by name, dates, type, or what they discuss (about). Lists files; with about, each with "
    "its best passage as an excerpt numbered [Source N]."
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
    """``find_files`` and ``get_file_info``.

    *retriever* is the one ``classic`` searches with; without it, ``find_files``
    can't search by subject.
    """

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        retriever: SourceRetriever | None = None,
        topic_tags: TopicTagRepository | None = None,
        candidate_limit: int = CANDIDATE_LIMIT,
    ) -> None:
        self._repo = document_repo
        self._retriever = retriever
        self._topic_tags = topic_tags
        self._candidate_limit = candidate_limit

    @tool("find_files", _FIND_FILES)
    async def find_files(
        self,
        ctx: ToolContext,
        name: Annotated[
            str | None,
            Field(max_length=300, description='Words from the file name, not a sentence, e.g. "contrat acme 2024".'),
        ] = None,
        about: Annotated[
            str | None,
            Field(
                max_length=500,
                description=(
                    "What the files discuss, as a full question or sentence in the documents' language, "
                    'abbreviations spelled out, e.g. "Which reports assess the cost of the cloud migration?". '
                    "Ranks the files by their passages about it."
                ),
            ),
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
            Field(
                description="By match (the default with a name or about), document date, or upload date (otherwise)."
            ),
        ] = None,
        oldest_first: Annotated[bool, Field(description="Oldest first when sorting by a date.")] = False,
        limit: Annotated[int, Field(ge=1, le=25, description="How many files to list at most.")] = 10,
    ) -> str:
        """The scope's files that match *name* and pass the filters, at most *limit*, each with its details.

        Without *about*, a catalog lookup: the best name match first with a
        *name*, the latest uploads first without one. With *about*, the files
        whose passages are about it (``_find_by_subject``).
        """
        partitions = ctx.scope.partitions
        if not partitions:
            raise ToolError("This conversation has no documents.")
        for after, before, which in (
            (created_after, created_before, "created"),
            (uploaded_after, uploaded_before, "uploaded"),
        ):
            if after and before and after >= before:
                raise ToolError(f"{which}_after must be earlier than {which}_before.")

        name = (name or "").strip()
        about = (about or "").strip()
        created = (created_after, created_before)
        uploaded = (uploaded_after, uploaded_before)
        if about:
            return await self._find_by_subject(
                ctx,
                about,
                name=name,
                file_types=file_types,
                created=created,
                uploaded=uploaded,
                sort=sort_by if sort_by in ("created", "uploaded") else "match",
                oldest_first=oldest_first,
                limit=limit,
            )

        files, truncated = await self._catalog(ctx)
        sort = sort_by if sort_by and (sort_by != "match" or name) else ("match" if name else "uploaded")
        # Up to CANDIDATE_LIMIT files to filter and match: CPU work, kept off
        # the event loop so the other requests of this process aren't held up.
        found, unmatched = await asyncio.to_thread(
            _select,
            files,
            name=name,
            file_types=file_types,
            created=created,
            uploaded=uploaded,
            sort=sort,
            oldest_first=oldest_first,
        )
        notes = self._notes(unmatched, truncated)
        if not found:
            hint = "Try fewer words, other filters, or about to find files by what they discuss."
            return " ".join(["No file matched.", *notes, hint])
        order = "best name match first" if sort == "match" else _date_order(sort, oldest_first)
        lines = [f"{min(limit, len(found))} of {len(found)} files, {order}.", *notes]
        for number, f in enumerate(found[:limit], 1):
            lines.append(f"{number}. {one_line(_display_name(f))}")
            lines.append(f"   {_details(f)}")
        return "\n".join(lines)

    async def _find_by_subject(
        self,
        ctx: ToolContext,
        about: str,
        *,
        name: str,
        file_types: list[str] | None,
        created: tuple[date | None, date | None],
        uploaded: tuple[date | None, date | None],
        sort: SortBy,
        oldest_first: bool,
        limit: int,
    ) -> str:
        """The files whose passages are about *about*, within the filters, each ranked by its best passage.

        The name, type and upload date are in the catalog only, so they narrow
        the search to the files that pass them. The document's date is a filter
        of the search itself. Either way, the files found are checked against
        the catalog: a search whose dates match nothing is retried at any date.
        """
        if self._retriever is None:
            raise ToolError("Finding files by what they discuss isn't available here.")
        scope = ctx.scope
        dates = created_between(*created)
        notes: list[str] = []
        candidates: dict[tuple[str, str], CatalogFile] | None = None
        if name or file_types or any(uploaded):
            files, truncated = await self._catalog(ctx)
            selected, unmatched = await asyncio.to_thread(
                _select,
                files,
                name=name,
                file_types=file_types,
                created=created,
                uploaded=uploaded,
                sort="uploaded",
                oldest_first=False,
            )
            notes = self._notes(unmatched, truncated)
            if not selected:
                return " ".join(["No file matched.", *notes, "Try fewer words or other filters."])
            candidates = {(f.partition, f.file_id): f for f in selected}
            scope = replace(scope, file_ids=list(dict.fromkeys(f.file_id for f in selected)))
            dates = []  # the files that passed are within the dates
        search = SearchQueries(query_list=[Query(query=about, temporal_filters=dates or None)])
        try:
            chunks, _web = await self._retriever.retrieve(
                search,
                route=Route.DOCUMENTS,
                scope=scope,
                top_k=SUBJECT_PASSAGES,
                retrieval_top_k=SUBJECT_PASSAGES,
                overrides={"disable_reranker": True, "disable_expansion": True},
                trace=ctx.trace,
            )
        except PartitionNotFoundError as e:
            raise ToolError(e.message) from e

        # A file id is unique within its partition only.
        hits: dict[tuple[str, str], list[Chunk]] = {}
        for chunk in chunks:
            key = (chunk.partition, chunk.document_id)
            if candidates is None or key in candidates:
                hits.setdefault(key, []).append(chunk)
        catalog = candidates if candidates is not None else await self._catalog_of(hits)
        found = [
            (catalog[key], passages)
            for key, passages in hits.items()
            if key in catalog and _passes(catalog[key], file_types=file_types, created=created, uploaded=uploaded)
        ]
        if not found:
            if not chunks:
                advice = "the subject reworded, with the terms the documents would use"
                return " ".join(
                    ["No passage is about it.", *notes, f"Try {advice}{', or wider dates' if any(created) else ''}."]
                )
            return " ".join(["No file within the filters has passages about it.", *notes, "Try wider filters."])
        if sort != "match":
            by_date = _sorted([f for f, _ in found], sort, oldest_first)
            order = {(f.partition, f.file_id): i for i, f in enumerate(by_date)}
            found.sort(key=lambda hit: order[(hit[0].partition, hit[0].file_id)])

        heading = (
            f"{min(limit, len(found))} of {len(found)} files with passages about it, "
            f"{'best match first' if sort == 'match' else _date_order(sort, oldest_first)}; "
            "under each, its best passage as an excerpt, which read_passage shows in full."
        )
        lines = [heading, *notes]
        for number, (f, passages) in enumerate(found[:limit], 1):
            best = passages[0]
            lines.append(f"{number}. {one_line(_display_name(f))}")
            lines.append(f"   {_details(f)}")
            lines.append(f"   Matching passages: {len(passages)}. The best:")
            lines.append(f"[Source {ctx.sources.number(best, key=best.id)}]")
            lines.append(f"   {neutralize_prompt_control_tokens(excerpt(best.text, BEST_PASSAGE_CHARS))}")
        return "\n".join(lines)

    async def _catalog(self, ctx: ToolContext) -> tuple[list[CatalogFile], bool]:
        """The scope's files, at most ``candidate_limit``, the latest uploads first, and whether there were more."""
        partitions = ctx.scope.partitions or []
        files = await self._repo.list_catalog_files(
            None if "all" in partitions else partitions,
            file_ids=ctx.scope.file_ids,
            limit=self._candidate_limit + 1,
        )
        return files[: self._candidate_limit], len(files) > self._candidate_limit

    async def _catalog_of(self, keys) -> dict[tuple[str, str], CatalogFile]:
        """The catalog entries of the (partition, file id) *keys*."""
        if not keys:
            return {}
        partitions = sorted({partition for partition, _ in keys})
        file_ids = sorted({file_id for _, file_id in keys})
        rows = await self._repo.list_catalog_files(partitions, file_ids=file_ids, limit=len(partitions) * len(file_ids))
        return {(f.partition, f.file_id): f for f in rows if (f.partition, f.file_id) in keys}

    def _notes(self, unmatched: list[str], truncated: bool) -> list[str]:
        notes = []
        if unmatched:
            notes.append(f"No file name has: {', '.join(unmatched)}.")
        if truncated:
            notes.append(f"Only the {self._candidate_limit} most recently uploaded files were looked at.")
        return notes

    @tool("get_file_info", _GET_FILE_INFO)
    async def get_file_info(
        self,
        ctx: ToolContext,
        partition: Annotated[str, Field(description="The partition shown with the file.")],
        file_id: Annotated[str, Field(description="The file id shown with the file.")],
    ) -> str:
        """One file of the scope: its names, details, topics, failed indexing steps and other metadata.

        Read from its catalog row, its upload metadata and its topic tags,
        never from its passages. Refused for a file outside the scope.
        """
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
    files = [f for f in files if _passes(f, file_types=file_types, created=created, uploaded=uploaded)]
    unmatched: list[str] = []
    if not name:
        found = files
    elif exact := [f for f in files if f.file_id == name]:
        found = exact
    else:
        ranking = rank_by_name(name, [_names(f) for f in files])
        found = [files[m.index] for m in ranking.matches]
        unmatched = ranking.unmatched
    return _sorted(found, sort, oldest_first), unmatched


def _passes(
    f: CatalogFile,
    *,
    file_types: list[str] | None,
    created: tuple[date | None, date | None],
    uploaded: tuple[date | None, date | None],
) -> bool:
    """Whether *f* passes the filters; an undated document fails a filter on the document's date."""
    return (
        _within(_created(f), *created, required=any(created))
        and _within(_uploaded(f), *uploaded, required=False)
        and (not file_types or _has_type(f, file_types))
    )


def _sorted(files: list[CatalogFile], sort: SortBy, oldest_first: bool) -> list[CatalogFile]:
    """*files* by document date (undated last) or upload date; in their own order by ``match``."""
    if sort == "created":
        dated = sorted((f for f in files if _created(f)), key=_created, reverse=not oldest_first)
        return dated + [f for f in files if not _created(f)]
    if sort == "uploaded":
        return sorted(files, key=_uploaded, reverse=not oldest_first)
    return files


def _date_order(sort: SortBy, oldest_first: bool) -> str:
    which = "by document date" if sort == "created" else "by upload date"
    return f"{which}, {'oldest' if oldest_first else 'newest'} first"


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
