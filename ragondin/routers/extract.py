from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from utils.dependencies import Indexer, get_indexer, vectordb
from utils.logger import get_logger

logger = get_logger()

# Create an APIRouter instance
router = APIRouter()


@router.get("/{extract_id}")
async def get_extract(extract_id: str, indexer: Indexer = Depends(get_indexer)):
    log = logger.bind(extract_id=extract_id)
    try:
        doc = vectordb.get_chunk_by_id(extract_id)
        if doc is None:
            log.warning("Extract not found.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Extract '{extract_id}' not found.",
            )
        log.info("Extract successfully retrieved.")
    except Exception:
        log.exception("Failed to retrieve extract.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve extract.",
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"page_content": doc.page_content, "metadata": doc.metadata},
    )
