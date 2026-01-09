from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_vectordb
from utils.logger import get_logger

from .utils import current_user_or_admin_partitions_list

logger = get_logger()

# Create an APIRouter instance
router = APIRouter()


@router.get("/{extract_id}",
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
  - `datetime`: Document date (if set)
  - `modified_at`: File modification timestamp
  - `created_at`: File creation timestamp
  - `indexed_at`: Chunk indexing timestamp
  - Additional custom metadata

**Use Case:**
View detailed content of a specific chunk from search results.
""",
)
async def get_extract(
    request: Request,
    extract_id: str,
    vectordb=Depends(get_vectordb),
    user_partitions=Depends(current_user_or_admin_partitions_list),
):
    log = logger.bind(extract_id=extract_id)
    try:
        chunk = await vectordb.get_chunk_by_id.remote(extract_id)
        if chunk is None:
            log.warning("Extract not found.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Extract '{extract_id}' not found.",
            )
        chunk_partition = chunk.metadata["partition"]
        log.info(
            f"User partitions: {user_partitions}, Chunk partition: {chunk_partition}"
        )
        if chunk_partition not in user_partitions and user_partitions != ["all"]:
            log.warning("User does not have access to this extract.")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User does not have access to extract '{extract_id}'.",
            )
        log.info("Extract successfully retrieved.")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to retrieve extract.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve extract: {str(e)}",
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"page_content": chunk.page_content, "metadata": chunk.metadata},
    )
