from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_indexer
from utils.logger import get_logger

from .utils import (
    current_user_or_admin_partitions_list,
    require_partition_viewer,
    require_partitions_viewer,
)

logger = get_logger()

router = APIRouter()


@router.get(
    "",
    description="""Perform semantic search across multiple partitions.

**Query Parameters:**
- `partitions`: List of partition names (default: ["all"])
- `text`: Search query text (required)
- `top_k`: Number of results to return (default: 5)

**Behavior:**
- `partitions=["all"]`: Search all accessible partitions
- Specific partitions: Search only those partitions
- Uses vector similarity for semantic search

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
""",
)
async def search_multiple_partitions(
    request: Request,
    partitions: list[str] | None = Query(default=["all"], description="List of partitions to search"),
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    indexer=Depends(get_indexer),
    partition_viewer=Depends(require_partitions_viewer),
    user_partitions=Depends(current_user_or_admin_partitions_list),
):
    # Fetch user partitions if "all" is specified, or all partitions if super admin
    if partitions == ["all"]:
        partitions = user_partitions

    log = logger.bind(partitions=partitions, query=text, top_k=top_k)

    results = await indexer.asearch.remote(query=text, top_k=top_k, partition=partitions)
    log.info(
        "Semantic search on multiple partitions completed.",
        result_count=len(results),
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

**Permissions:**
- Requires viewer role on the partition

**Response:**
Returns matching documents with:
- `content`: Document chunk text
- `metadata`: File and chunk metadata (file_id, filename, page, timestamps, etc.)
- `link`: URL to detailed chunk view

**Use Case:**
Search within a specific document collection or project partition.
""",
)
async def search_one_partition(
    request: Request,
    partition: str,
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    indexer=Depends(get_indexer),
    partition_viewer=Depends(require_partition_viewer),
):
    log = logger.bind(partition=partition, query=text, top_k=top_k)
    results = await indexer.asearch.remote(query=text, top_k=top_k, partition=partition)
    log.info("Semantic search on single partition completed.", result_count=len(results))
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
    partition_viewer=Depends(require_partition_viewer),
):
    log = logger.bind(partition=partition, file_id=file_id, query=text, top_k=top_k)
    results = await indexer.asearch.remote(query=text, top_k=top_k, partition=partition, filter={"file_id": file_id})
    log.info("Semantic search on specific file completed.", result_count=len(results))

    documents = [
        {
            "link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"])),
            "metadata": doc.metadata,
            "content": doc.page_content,
        }
        for doc in results
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={"documents": documents})
