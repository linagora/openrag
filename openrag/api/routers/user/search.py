"""Semantic search routes — thin HTTP layer over RetrievalService.

Phase 8C.1: the retrieval call (was ``indexer.asearch.remote`` + the
legacy ``_expand_with_related_chunks``) moved to
``services.orchestrators.retrieval_service.RetrievalService.search``.
This module keeps HTTP transport only: request-scoped authorization,
partition resolution from the authenticated user, the byte-identical
workspace-not-found 404 guard, ``request.url_for`` links, and response
shaping (domain ``Chunk`` → ``{link, metadata, content}``).
"""

from typing import Annotated

from api.dependencies.auth import (
    current_user_or_admin_partitions_list,
    require_partition_viewer,
    require_partitions_viewer,
)
from api.dependencies.files import validate_file_id
from core.utils.filter_validation import validate_search_filter
from core.utils.logging import get_logger
from di.providers import get_retrieval_service, get_workspace_service
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

logger = get_logger()

router = APIRouter()


class RelatedDocSearchParams:
    def __init__(
        self,
        include_related: bool = Query(False, description="Include chunks from files with same relationship_id"),
        include_ancestors: bool = Query(False, description="Include chunks from ancestor files in hierarchy"),
        related_limit: int = Query(
            20, ge=0, description="Maximum number of related/ancestor chunks to fetch per result"
        ),
        max_ancestor_depth: int | None = Query(
            None, ge=0, description="Maximum depth of ancestor files to include. None means unlimited."
        ),
    ):
        self.include_related = include_related
        self.include_ancestors = include_ancestors
        self.related_limit = related_limit
        self.max_ancestor_depth = max_ancestor_depth


class CommonSearchParams:
    def __init__(
        self,
        text: str = Query(..., description="Text to search semantically"),
        top_k: int = Query(5, ge=1, description="Number of top results to return"),
        similarity_threshold: float = Query(
            0.75, ge=0, le=1, description="Minimum similarity score for results (0 to 1)"
        ),
        filter: str | None = Query(
            default=None,
            description="""Milvus filter expression string.""",
        ),
    ):
        # Reject filter expressions that could break out of the partition
        # scope (unbalanced parens rebalancing the `(partition …) and (…)`
        # wrapper) before the raw string reaches the store. Raises 400.
        validate_search_filter(filter)
        self.text = text
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.filter = filter


def _documents(request: Request, chunks) -> list[dict]:
    docs: list[dict] = []
    for c in chunks:
        # Restore the legacy response metadata shape. Chunk.from_langchain
        # lifts file_id / partition / page / _id out of the free-form
        # metadata into typed Chunk fields; to_langchain merges them back so
        # the API contract (metadata.file_id, _id, …) matches the
        # pre-Phase-8 router that returned the raw Document metadata.
        meta = c.to_langchain().metadata
        docs.append(
            {
                "link": str(request.url_for("get_extract", extract_id=meta.get("_id") or c.id)),
                "metadata": meta,
                "content": c.text,
            }
        )
    return docs


@router.get(
    "",
    description="""Perform semantic search across multiple partitions.

**Query Parameters:**
- `partitions`: List of partition names (default: ["all"])
- `text`: Search query text (required)
- `top_k`: Number of results to return (default: 5)
- similarity_threshold: Minimum similarity score for results (0 to 1, default: 0.75)
- `include_related`: Include chunks from files with same relationship_id (default: false)
- `include_ancestors`: Include chunks from ancestor files in hierarchy (default: false)
- `related_limit`: Maximum number of related/ancestor chunks to fetch per result (default: 20). This is used when `include_related` or `include_ancestors` is true.
- `max_ancestor_depth`: Maximum depth of ancestor files to include. None means unlimited. (default: None)
- `filter`: Milvus filter expression string for additional filtering (optional)
    Milvus supports the following operators:
    - Comparison: ==, !=, >, <, >=, <=
    - Range: IN, LIKE
    - Logical: AND, OR, NOT (see https://milvus.io/docs/boolean.md)
    Examples:
    - `file_id == "abc123"`
    - `created_at > ISO "2024-01-01T00:00:00+00:00"`
    - `page >= 5 AND page <= 10`
    - `file_id in ["id1", "id2", "id3"]`

**Behavior:**
- `partitions=["all"]`: Search all accessible partitions
- Specific partitions: Search only those partitions
- Uses vector similarity for semantic search
- When `include_related=true`: Expands results to include all chunks from files
  that share the same relationship_id (e.g., email thread, folder contents)
- When `include_ancestors=true`: Expands results to include chunks from parent
  files in the document hierarchy (e.g., parent emails in thread)

**Permissions:**
- Requires viewer role on specified partitions
- Regular users: Limited to their assigned partitions
- Admins: Can search any partition

**Response:**
Returns matching documents with:
- `content`: Document chunk text
- `metadata`: File and chunk metadata
- `link`: URL to detailed chunk view

**Use Case:**
Find relevant information across your entire document collection.
Use relationship expansion for context-aware retrieval in email threads or folder structures.
""",
)
async def search_multiple_partitions(
    request: Request,
    search_params: Annotated[CommonSearchParams, Depends()],
    related_params: Annotated[RelatedDocSearchParams, Depends()],
    partitions: list[str] | None = Query(default=["all"], description="List of partitions to search"),
    workspace: str | None = Query(None, description="Workspace ID to filter results"),
    partition_viewer=Depends(require_partitions_viewer),
    user_partitions=Depends(current_user_or_admin_partitions_list),
    service=Depends(get_retrieval_service),
    workspaces=Depends(get_workspace_service),
):
    if partitions == ["all"]:
        partitions = user_partitions
    if not partitions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No accessible partitions")

    log = logger.bind(
        partitions=partitions,
        query_len=len(search_params.text),
        top_k=search_params.top_k,
        workspace=workspace,
        include_related=related_params.include_related,
        include_ancestors=related_params.include_ancestors,
    )

    filter_params = None
    if workspace:
        ws = await workspaces.get_workspace(workspace)
        if not ws or ws["partition_name"] not in partitions:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
        filter_params = {"workspace_id": workspace}

    results = await service.search(
        text=search_params.text,
        partitions=partitions,
        top_k=search_params.top_k,
        similarity_threshold=search_params.similarity_threshold,
        filter=search_params.filter,
        filter_params=filter_params,
        include_related=related_params.include_related,
        include_ancestors=related_params.include_ancestors,
        related_limit=related_params.related_limit,
        max_ancestor_depth=related_params.max_ancestor_depth,
    )
    log.info("Semantic search on multiple partitions completed.", result_count=len(results))

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"documents": _documents(request, results)},
    )


@router.get(
    "/partition/{partition}",
    description="""Perform semantic search within a single partition.

**Parameters:**
- `partition`: The partition name to search

**Query Parameters:**
- `text`: Search query text (required)
- `top_k`: Number of results to return (default: 5)
- similarity_threshold: Minimum similarity score for results (0 to 1, default: 0.75)
- `include_related`: Include chunks from files with same relationship_id (default: false)
- `include_ancestors`: Include chunks from ancestor files in hierarchy (default: false)
- `related_limit`: Maximum number of related/ancestor chunks to fetch per result (default: 20). This is used when `include_related` or `include_ancestors` is true.
- `max_ancestor_depth`: Maximum depth of ancestor files to include. None means unlimited. (default: None)
- `filter`: Milvus filter expression string for additional filtering (optional)
    Milvus supports the following operators:
    - Comparison: ==, !=, >, <, >=, <=
    - Range: IN, LIKE
    - Logical: AND, OR, NOT (see https://milvus.io/docs/boolean.md)
    Examples:
    - `file_id == "abc123"`
    - `created_at > ISO "2024-01-01T00:00:00+00:00"`
    - `page >= 5 AND page <= 10`
    - `file_id in ["id1", "id2", "id3"]`

**Permissions:**
- Requires viewer role on the partition

**Response:**
Returns matching documents with:
- `content`: Document chunk text
- `metadata`: File and chunk metadata (file_id, filename, page, timestamps, etc.)
- `link`: URL to detailed chunk view

**Use Case:**
Search within a specific document collection or project partition.
Use relationship expansion for context-aware retrieval in email threads or folder structures.
""",
)
async def search_one_partition(
    request: Request,
    partition: str,
    search_params: Annotated[CommonSearchParams, Depends()],
    related_params: Annotated[RelatedDocSearchParams, Depends()],
    workspace: str | None = Query(None, description="Workspace ID to filter results"),
    partition_viewer=Depends(require_partition_viewer),
    service=Depends(get_retrieval_service),
    workspaces=Depends(get_workspace_service),
):
    log = logger.bind(
        partition=partition,
        query_len=len(search_params.text),
        top_k=search_params.top_k,
        workspace=workspace,
        include_related=related_params.include_related,
        include_ancestors=related_params.include_ancestors,
    )
    filter_params = None
    if workspace:
        ws = await workspaces.get_workspace(workspace)
        if not ws or ws["partition_name"] != partition:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
        filter_params = {"workspace_id": workspace}

    results = await service.search(
        text=search_params.text,
        partitions=partition,
        top_k=search_params.top_k,
        similarity_threshold=search_params.similarity_threshold,
        filter=search_params.filter,
        filter_params=filter_params,
        include_related=related_params.include_related,
        include_ancestors=related_params.include_ancestors,
        related_limit=related_params.related_limit,
        max_ancestor_depth=related_params.max_ancestor_depth,
    )
    log.info("Semantic search on single partition completed.", result_count=len(results))

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"documents": _documents(request, results)},
    )


@router.get(
    "/partition/{partition}/file/{file_id}",
    description="""Perform semantic search within a specific file.

**Parameters:**
- `partition`: The partition name
- `file_id`: The file identifier

**Query Parameters:**
- `text`: Search query text (required)
- `top_k`: Number of results to return (default: 5)
- similarity_threshold: Minimum similarity score for results (0 to 1, default: 0.75)
- `filter`: Milvus filter expression string for additional filtering (optional)
    Milvus supports the following operators:
    - Comparison: ==, !=, >, <, >=, <=
    - Range: IN, LIKE
    - Logical: AND, OR, NOT (see https://milvus.io/docs/boolean.md)
    Examples:
    - `file_id == "abc123"`
    - `created_at > ISO "2024-01-01T00:00:00+00:00"`
    - `page >= 5 AND page <= 10`
    - `file_id in ["id1", "id2", "id3"]`

**Permissions:**
- Requires viewer role on the partition

**Response:**
Returns matching chunks from the file with:
- `content`: Chunk text content
- `metadata`: Chunk metadata (page number, timestamps, etc.)
- `link`: URL to detailed chunk view

**Use Case:**
Find specific information within a single document using semantic search.
""",
)
async def search_file(
    request: Request,
    partition: str,
    search_params: Annotated[CommonSearchParams, Depends()],
    file_id: str = Depends(validate_file_id),
    partition_viewer=Depends(require_partition_viewer),
    service=Depends(get_retrieval_service),
):
    log = logger.bind(
        partition=partition, file_id=file_id, query_len=len(search_params.text), top_k=search_params.top_k
    )

    filter = "file_id == {_file_id}" + (f" AND {search_params.filter}" if search_params.filter else "")
    params = {"_file_id": file_id}

    results = await service.search(
        text=search_params.text,
        partitions=partition,
        top_k=search_params.top_k,
        similarity_threshold=search_params.similarity_threshold,
        filter=filter,
        filter_params=params,
    )
    log.info("Semantic search on specific file completed.", result_count=len(results))

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"documents": _documents(request, results)},
    )
