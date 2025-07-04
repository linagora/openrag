from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from utils.dependencies import Indexer, get_indexer
from utils.logger import get_logger

logger = get_logger()

router = APIRouter()


@router.get("")
async def search_multiple_partitions(
    request: Request,
    partitions: Optional[List[str]] = Query(
        default=["all"], description="List of partitions to search"
    ),
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    indexer: Indexer = Depends(get_indexer),
):
    log = logger.bind(partitions=partitions, query=text, top_k=top_k)
    try:
        results = await indexer.asearch.remote(
            query=text, top_k=top_k, partition=partitions
        )
        log.info(
            "Semantic search on multiple partitions completed.",
            result_count=len(results),
        )
    except ValueError as e:
        log.warning(f"Invalid input: {str(e)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        log.exception("Search across multiple partitions failed.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Search failed."
        )

    documents = [
        {"link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"]))}
        for doc in results
    ]

    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"documents": documents}
    )


@router.get("/partition/{partition}")
async def search_one_partition(
    request: Request,
    partition: str,
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    indexer: Indexer = Depends(get_indexer),
):
    log = logger.bind(partition=partition, query=text, top_k=top_k)
    try:
        results = await indexer.asearch.remote(
            query=text, top_k=top_k, partition=partition
        )
        log.info(
            "Semantic search on single partition completed.", result_count=len(results)
        )
    except ValueError as e:
        log.warning(f"Invalid input: {str(e)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        log.exception("Search on partition failed.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Search failed."
        )

    documents = [
        {
            "link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"])),
            "metadata": doc.metadata,
            "content": doc.page_content
        }
        for doc in results
    ]

    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"documents": documents}
    )


@router.get("/partition/{partition}/file/{file_id}")
async def search_file(
    request: Request,
    partition: str,
    file_id: str,
    text: str = Query(..., description="Text to search semantically"),
    top_k: int = Query(5, description="Number of top results to return"),
    indexer: Indexer = Depends(get_indexer),
):
    log = logger.bind(partition=partition, file_id=file_id, query=text, top_k=top_k)
    try:
        results = await indexer.asearch.remote(
            query=text, top_k=top_k, partition=partition, filter={"file_id": file_id}
        )
        log.info(
            "Semantic search on specific file completed.", result_count=len(results)
        )
    except ValueError as e:
        log.warning(f"Invalid input: {str(e)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        log.exception("Search on file failed.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Search failed."
        )

    documents = [
        {"link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"]))}
        for doc in results
    ]

    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"documents": documents}
    )
