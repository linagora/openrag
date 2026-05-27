"""Extract route — thin HTTP layer over :class:`ConversionService`.

Phase 8E: the chunk-by-id lookup moved to
``services.orchestrators.conversion_service.ConversionService`` (clean
``VectorStore`` port, no Ray). This module keeps HTTP transport only:
the request-scoped partition authorization and the not-found / forbidden
guards whose exact ``{"detail": ...}`` body the legacy endpoint
returned via ``HTTPException``.
"""

from api.dependencies.auth import current_user_or_admin_partitions_list
from di.providers import get_conversion_service
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from utils.logger import get_logger

logger = get_logger()

router = APIRouter()


@router.get(
    "/{extract_id}",
    description="""Get a specific document chunk by its ID.

**Parameters:**
- `extract_id`: The unique chunk identifier (from search or list results)

**Permissions:**
- Requires access to the partition containing the chunk
- Regular users: Only chunks from assigned partitions
- Admins: Any chunk

**Response:**
Returns chunk details including:
- `page_content`: The text content of the chunk
- `metadata`: Chunk metadata including:
  - `file_id`: Source file identifier
  - `filename`: Original filename
  - `partition`: Partition name
  - `page`: Page number in source document
  - `indexed_at`: Chunk indexing timestamp
  - Additional custom metadata

**Use Case:**
View detailed content of a specific chunk from search results.
""",
)
async def get_extract(
    extract_id: str,
    user_partitions=Depends(current_user_or_admin_partitions_list),
    service=Depends(get_conversion_service),
):
    log = logger.bind(extract_id=extract_id)

    chunk = await service.get_chunk(extract_id)
    if chunk is None:
        log.warning("Extract not found.")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Extract '{extract_id}' not found.",
        )
    chunk_partition = chunk.get("metadata", {}).get("partition")
    if not chunk_partition:
        log.warning("Extract metadata missing partition.")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Extract '{extract_id}' not found.",
        )
    log.info(f"User partitions: {user_partitions}, Chunk partition: {chunk_partition}")
    if chunk_partition not in user_partitions and user_partitions != ["all"]:
        log.warning("User does not have access to this extract.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not have access to extract '{extract_id}'.",
        )
    log.info("Extract successfully retrieved.")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"page_content": chunk["page_content"], "metadata": chunk["metadata"]},
    )
