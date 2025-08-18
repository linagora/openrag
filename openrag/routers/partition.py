from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_indexer, get_vectordb
from utils.logger import get_logger

logger = get_logger()
router = APIRouter()

indexer = get_indexer()
vectordb = get_vectordb()


@router.get("/")
async def list_existant_partitions():
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
async def delete_partition(partition: str):
    try:
        deleted = await vectordb.delete_partition.remote(partition)
    except Exception:
        logger.exception("Failed to delete partition", partition=partition)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete partition",
        )

    if not deleted:
        logger.warning("Partition not found for deletion", partition=partition)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Partition not found",
        )

    logger.debug("Partition successfully deleted.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{partition}")
async def list_files(request: Request, partition: str, limit: int | None = None):
    log = logger.bind(partition=partition)

    if not await vectordb.partition_exists.remote(partition):
        log.warning("Partition not found")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Partition '{partition}' not found",
        )

    try:
        partition_dict = await vectordb.list_partition_files.remote(
            partition=partition, limit=limit
        )
        log.debug(
            "Listed files in partition", file_count=len(partition_dict.get("files", []))
        )
    except ValueError as e:
        log.warning(f"Invalid partition value: {str(e)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        log.exception("Failed to list files in partition")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list files: {str(e)}",
        )

    def process_file(file_dict):
        return {
            "link": str(
                request.url_for(
                    "get_file", partition=partition, file_id=file_dict["file_id"]
                )
            ),
            **file_dict,
        }

    partition_dict["files"] = list(map(process_file, partition_dict.get("files", [])))
    return JSONResponse(status_code=status.HTTP_200_OK, content=partition_dict)


@router.get("/{partition}/file/{file_id}")
async def get_file(
    request: Request,
    partition: str,
    file_id: str,
):
    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File '{file_id}' not found in partition '{partition}'.",
        )

    try:
        results = await vectordb.get_file_chunks.remote(
            partition=partition, file_id=file_id, include_id=True
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch file chunks.",
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
    request: Request, partition: str, include_embedding: bool = True
):
    # Check if partition exists
    if not await vectordb.partition_exists.remote(partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Partition '{partition}' not found.",
        )

    try:
        chunks = await vectordb.list_all_chunk.remote(
            partition=partition, include_embedding=include_embedding
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
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


# @router.get("/{partition}/sample")
# async def sample_chunks(
#     request: Request, partition: str, n_ids: int = 200, seed: int | None = None
# ):
#     # Check if partition exists
#     if not await vectordb.partition_exists.remote(partition):
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail=f"Partition '{partition}' not found.",
#         )

#     try:
#         list_ids = await vectordb.sample_chunk_ids.remote(
#             partition=partition, n_ids=n_ids, seed=seed
#         )
#     except ValueError as e:
#         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
#     except Exception as e:
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
#         )

#     chunks = [
#         {"link": str(request.url_for("get_extract", extract_id=id))} for id in list_ids
#     ]

#     return JSONResponse(status_code=status.HTTP_200_OK, content={"chunk_urls": chunks})


# @router.get("/check-file/{partition}/file/{file_id}")
# async def check_file_exists_in_partition(
#     partition: str,
#     file_id: str,
# ):
#     log = logger.bind(partition=partition, file_id=file_id)
#     exists = await vectordb.file_exists.remote(file_id, partition)
#     if not exists:
#         log.warning("File not found in partition.")
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail=f"File '{file_id}' not found in partition '{partition}'.",
#         )

#     log.debug("File exists in partition.")
#     return JSONResponse(
#         status_code=status.HTTP_200_OK,
#         content=f"File '{file_id}' exists in partition '{partition}'.",
#     )


# def process_partition(partition):
#     def add_file_url(file_obj):
#         file_dict = file_obj.to_dict()
#         file_metadata = file_dict.pop("file_metadata", {})
#         return {
#             **file_dict,
#             **file_metadata,
#             "file_url": str(
#                 request.url_for(
#                     "get_file",
#                     partition=file_dict["partition"],
#                     file_id=file_dict["file_id"],
#                 )
#             ),
#         }
#     partition["files"] = list(map(add_file_url, partition["files"]))
#     return partition
