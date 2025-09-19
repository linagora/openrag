from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_vectordb
from utils.logger import get_logger

logger = get_logger()
router = APIRouter()


def _quote_param_value(s: str) -> str:
    return quote(s, safe="")


@router.get("/")
async def list_existant_partitions(vectordb=Depends(get_vectordb)):
    try:
        partitions = await vectordb.list_partitions.remote()
        logger.debug(
            "Returned list of existing partitions.", partition_count=len(partitions)
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"partitions": partitions}
        )
    except Exception as e:
        logger.exception("Failed to list partitions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list partitions: {str(e)}",
        )


@router.delete("/{partition}")
async def delete_partition(partition: str, vectordb=Depends(get_vectordb)):
    await vectordb.delete_partition.remote(partition)
    logger.debug("Partition successfully deleted.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{partition}")
async def list_files(
    request: Request,
    partition: str,
    limit: int | None = None,
    vectordb=Depends(get_vectordb),
):
    log = logger.bind(partition=partition)
    file_obj_l = await vectordb.list_partition_files.remote(
        partition=partition, limit=limit
    )
    file_dicts = file_obj_l.get("files", [])
    log.debug("Listed files in partition", file_count=len(file_dicts))

    def process_file(file_dict):
        return {
            "link": str(
                request.url_for(
                    "get_file",
                    partition=_quote_param_value(file_dict.get("partition")),
                    file_id=_quote_param_value(file_dict.get("file_id")),
                )
            ),
            **file_dict,
        }

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"files": list(map(process_file, file_dicts))},
    )


@router.get("/{partition}/file/{file_id}")
async def get_file(
    request: Request,
    partition: str,
    file_id: str,
    vectordb=Depends(get_vectordb),
):
    results = await vectordb.get_file_chunks.remote(
        partition=partition, file_id=file_id, include_id=True
    )

    documents = [
        {"link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"]))}
        for doc in results
    ]

    metadata = (
        {k: v for k, v in results[0].metadata.items() if k != "_id"} if results else {}
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"metadata": metadata, "documents": documents},
    )


@router.get("/{partition}/chunks")
async def list_all_chunks(
    request: Request,
    partition: str,
    include_embedding: bool = True,
    vectordb=Depends(get_vectordb),
):
    chunks = await vectordb.list_all_chunk.remote(
        partition=partition, include_embedding=include_embedding
    )
    chunks = [
        {
            "link": str(
                request.url_for("get_extract", extract_id=chunk.metadata["_id"])
            ),
            "content": chunk.page_content,
            "metadata": chunk.metadata,
        }
        for chunk in chunks
    ]
    return JSONResponse(status_code=status.HTTP_200_OK, content={"chunks": chunks})
