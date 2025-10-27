from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_vectordb
from utils.logger import get_logger

from .utils import (
    ROLE_HIERARCHY,
    partitions_with_details,
    require_partition_owner,
    require_partition_viewer,
)

logger = get_logger()
router = APIRouter()

RoleType = Literal[*list(ROLE_HIERARCHY.keys())]


def _quote_param_value(s: str) -> str:
    return quote(s, safe="")


@router.get("/")
async def list_existant_partitions(
    vectordb=Depends(get_vectordb),
    partitions=Depends(partitions_with_details),
):
    if len(partitions) == 1 and partitions[0]["partition"] == "all":
        partitions = await vectordb.list_partitions.remote()
    logger.debug(
        "Returned list of existing partitions.", partition_count=len(partitions)
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"partitions": partitions}
    )


@router.delete("/{partition}")
async def delete_partition(
    partition: str,
    vectordb=Depends(get_vectordb),
    partition_owner=Depends(require_partition_owner),
):
    await vectordb.delete_partition.remote(partition)
    logger.debug("Partition successfully deleted.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{partition}")
async def list_files(
    request: Request,
    partition: str,
    limit: int | None = None,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
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
    partition_viewer=Depends(require_partition_viewer),
):
    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )
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
    partition_viewer=Depends(require_partition_viewer),
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


@router.post("/{partition}")
async def create_partition(
    request: Request, partition: str, vectordb=Depends(get_vectordb)
):
    if await vectordb.partition_exists.remote(partition):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Partition '{partition}' already exists.",
        )
    user_id = request.state.user["id"]
    await vectordb.create_partition.remote(partition=partition, user_id=user_id)
    return Response(status_code=status.HTTP_201_CREATED)


@router.get("/{partition}/users")
async def list_partition_users(
    partition: str,
    vectordb=Depends(get_vectordb),
    partition_owner=Depends(require_partition_owner),
):
    """
    List all users who are members of the given partition.
    """
    log = logger.bind(partition=partition)

    members = await vectordb.list_partition_members.remote(partition=partition)

    log.debug("Returned list of partition members.", member_count=len(members))
    return JSONResponse(status_code=status.HTTP_200_OK, content={"members": members})


@router.post("/{partition}/users")
async def add_partition_user(
    partition: str,
    user_id: int = Form(...),
    role: RoleType = Form("viewer"),
    vectordb=Depends(get_vectordb),
    partition_owner=Depends(require_partition_owner),
):
    """
    Add a user as a member of the given partition.
    """
    log = logger.bind(partition=partition, user_id=user_id)

    await vectordb.add_partition_member.remote(
        partition=partition, user_id=user_id, role=role
    )

    log.debug("User added to partition successfully")
    return Response(status_code=status.HTTP_201_CREATED)


@router.delete("/{partition}/users/{user_id}")
async def remove_partition_user(
    partition: str,
    user_id: int,
    vectordb=Depends(get_vectordb),
    partition_owner=Depends(require_partition_owner),
):
    """
    Remove a user from the given partition.
    """
    log = logger.bind(partition=partition, user_id=user_id)

    await vectordb.remove_partition_member.remote(partition=partition, user_id=user_id)

    log.debug("User removed from partition successfully")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{partition}/users/{user_id}")
async def update_partition_user_role(
    partition: str,
    user_id: int,
    role: RoleType = Form(...),
    vectordb=Depends(get_vectordb),
    partition_owner=Depends(require_partition_owner),
):
    """
    Update a user's role in the given partition.
    """
    log = logger.bind(partition=partition, user_id=user_id, role=role)

    await vectordb.update_partition_member_role.remote(
        partition=partition, user_id=user_id, new_role=role
    )

    log.debug("User role updated successfully")
    return Response(status_code=status.HTTP_200_OK)
