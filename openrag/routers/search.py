from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_indexer, get_vectordb
from utils.logger import get_logger

from .utils import (
    current_user_or_admin_partitions_list,
    require_partition_viewer,
    require_partitions_viewer,
)

logger = get_logger()

router = APIRouter()


async def _expand_with_related_chunks(
    results: list,
    vectordb,
    include_related: bool,
    include_ancestors: bool,
    related_limit: int = 20,
) -> list:
    """
    Expand search results with related and/or ancestor chunks.

    Args:
        results: Initial search results (list of Documents)
        vectordb: VectorDB actor reference
        include_related: Include chunks from files with same relationship_id
        include_ancestors: Include chunks from ancestor files (parent chain)
        related_limit: Maximum number of additional chunks to fetch per expansion

    Returns:
        Expanded list of Documents (original + related/ancestor chunks)
    """
    if not results or (not include_related and not include_ancestors):
        return results

    # Track what we already have to avoid duplicates
    seen_ids = {doc.metadata.get("_id") for doc in results}
    expanded_results = list(results)

    # Collect unique relationship_ids and file_ids from results
    relationship_ids = set()
    file_infos = []  # List of (partition, file_id) tuples

    for doc in results:
        metadata = doc.metadata
        if include_related and metadata.get("relationship_id"):
            relationship_ids.add((metadata.get("partition"), metadata.get("relationship_id")))
        if include_ancestors:
            file_infos.append((metadata.get("partition"), metadata.get("file_id")))

    # Fetch related chunks by relationship_id
    if include_related:
        for partition, rel_id in relationship_ids:
            if partition and rel_id:
                try:
                    related_chunks = await vectordb.get_related_chunks.remote(
                        partition=partition,
                        relationship_id=rel_id,
                        limit=related_limit,
                    )
                    for chunk in related_chunks:
                        chunk_id = chunk.metadata.get("_id")
                        if chunk_id and chunk_id not in seen_ids:
                            seen_ids.add(chunk_id)
                            expanded_results.append(chunk)
                except Exception as e:
                    logger.warning(
                        "Failed to fetch related chunks",
                        relationship_id=rel_id,
                        error=str(e),
                    )

    # Fetch ancestor chunks
    if include_ancestors:
        for partition, file_id in file_infos:
            if partition and file_id:
                try:
                    ancestor_chunks = await vectordb.get_ancestor_chunks.remote(
                        partition=partition,
                        file_id=file_id,
                        limit=related_limit,
                    )
                    for chunk in ancestor_chunks:
                        chunk_id = chunk.metadata.get("_id")
                        if chunk_id and chunk_id not in seen_ids:
                            seen_ids.add(chunk_id)
                            expanded_results.append(chunk)
                except Exception as e:
                    logger.warning(
                        "Failed to fetch ancestor chunks",
                        file_id=file_id,
                        error=str(e),
                    )

    return expanded_results


@router.get(
    "",
    description="""Perform semantic search across multiple partitions.

**Query Parameters:**
- `partitions`: List of partition names (default: ["all"])
- `text`: Search query text (required)
- `top_k`: Number of results to return (default: 5)
- `include_related`: Include chunks from files with same relationship_id (default: false)
- `include_ancestors`: Include chunks from ancestor files in hierarchy (default: false)

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
    partitions: list[str] | None = Query(default=["all"], description="List of partitions to search"),
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    include_related: bool = Query(False, description="Include chunks from files with same relationship_id"),
    include_ancestors: bool = Query(False, description="Include chunks from ancestor files in hierarchy"),
    indexer=Depends(get_indexer),
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partitions_viewer),
    user_partitions=Depends(current_user_or_admin_partitions_list),
):
    # Fetch user partitions if "all" is specified, or all partitions if super admin
    if partitions == ["all"]:
        partitions = user_partitions

    log = logger.bind(
        partitions=partitions,
        query=text,
        top_k=top_k,
        include_related=include_related,
        include_ancestors=include_ancestors,
    )

    results = await indexer.asearch.remote(query=text, top_k=top_k, partition=partitions)
    log.info(
        "Semantic search on multiple partitions completed.",
        result_count=len(results),
    )

    # Expand with related/ancestor chunks if requested
    if include_related or include_ancestors:
        results = await _expand_with_related_chunks(
            results=results,
            vectordb=vectordb,
            include_related=include_related,
            include_ancestors=include_ancestors,
        )
        log.info(
            "Expanded results with related/ancestor chunks.",
            expanded_count=len(results),
        )

    documents = [
        {
            "link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"])),
            "metadata": doc.metadata,
            "content": doc.page_content,
        }
        for doc in results
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={"documents": documents})


@router.get(
    "/partition/{partition}",
    description="""Perform semantic search within a single partition.

**Parameters:**
- `partition`: The partition name to search

**Query Parameters:**
- `text`: Search query text (required)
- `top_k`: Number of results to return (default: 5)
- `include_related`: Include chunks from files with same relationship_id (default: false)
- `include_ancestors`: Include chunks from ancestor files in hierarchy (default: false)

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
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    include_related: bool = Query(False, description="Include chunks from files with same relationship_id"),
    include_ancestors: bool = Query(False, description="Include chunks from ancestor files in hierarchy"),
    indexer=Depends(get_indexer),
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    log = logger.bind(
        partition=partition,
        query=text,
        top_k=top_k,
        include_related=include_related,
        include_ancestors=include_ancestors,
    )
    results = await indexer.asearch.remote(query=text, top_k=top_k, partition=partition)
    log.info("Semantic search on single partition completed.", result_count=len(results))

    # Expand with related/ancestor chunks if requested
    if include_related or include_ancestors:
        results = await _expand_with_related_chunks(
            results=results,
            vectordb=vectordb,
            include_related=include_related,
            include_ancestors=include_ancestors,
        )
        log.info(
            "Expanded results with related/ancestor chunks.",
            expanded_count=len(results),
        )

    documents = [
        {
            "link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"])),
            "metadata": doc.metadata,
            "content": doc.page_content,
        }
        for doc in results
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={"documents": documents})


@router.get(
    "/partition/{partition}/file/{file_id}",
    description="""Perform semantic search within a specific file.

**Parameters:**
- `partition`: The partition name
- `file_id`: The file identifier

**Query Parameters:**
- `text`: Search query text (required)
- `top_k`: Number of results to return (default: 5)

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
    file_id: str,
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    indexer=Depends(get_indexer),
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    log = logger.bind(
        partition=partition,
        file_id=file_id,
        query=text,
        top_k=top_k,
        include_related=False,
        include_ancestors=False,
    )
    results = await indexer.asearch.remote(query=text, top_k=top_k, partition=partition, filter={"file_id": file_id})
    log.info("Semantic search on specific file completed.", result_count=len(results))

    # Expand with related/ancestor chunks if requested
    results = await _expand_with_related_chunks(
        results=results,
        vectordb=vectordb,
        include_related=False,
        include_ancestors=False,
    )

    log.info(
        "Expanded results with related/ancestor chunks.",
        results=len(results),
    )

    documents = [
        {
            "link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"])),
            "metadata": doc.metadata,
            "content": doc.page_content,
        }
        for doc in results
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={"documents": documents})
