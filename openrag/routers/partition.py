from typing import Literal
from urllib.parse import quote

from components.app.service import OpenRAGApplicationService
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
app_service = OpenRAGApplicationService()

RoleType = Literal[*list(ROLE_HIERARCHY.keys())]


def _quote_param_value(s: str) -> str:
    return quote(s, safe="")


@router.get(
    "/",
    description="""List all accessible partitions.

**Response:**
Returns a list of partitions you have access to, including:
- `partition`: Partition name
- `created_at`: Creation timestamp
- Additional partition metadata

**Note:** Admins see all partitions; regular users see only their assigned partitions.
""",
)
async def list_existant_partitions(
    vectordb=Depends(get_vectordb),
    partitions=Depends(partitions_with_details),
):
    allowed = [p["partition"] for p in partitions]
    result = await app_service.list_partitions(allowed_partitions=allowed)
    partitions = result["partitions"]
    logger.debug("Returned list of existing partitions.", partition_count=len(partitions))
    return JSONResponse(status_code=status.HTTP_200_OK, content={"partitions": partitions})


@router.delete(
    "/{partition}",
    description="""Delete a partition and all its contents.

**Parameters:**
- `partition`: The partition name to delete

**Permissions:**
- Requires partition owner role

**Warning:**
This permanently deletes the partition and all its documents. This action cannot be undone.

**Response:**
Returns 204 No Content on successful deletion.
""",
)
async def delete_partition(
    partition: str,
    vectordb=Depends(get_vectordb),
    partition_owner=Depends(require_partition_owner),
):
    await app_service.delete_partition(vectordb=vectordb, partition=partition)
    logger.debug("Partition successfully deleted.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{partition}",
    description="""List all files in a partition.

**Parameters:**
- `partition`: The partition name
- `limit`: Optional maximum number of files to return

**Response:**
Returns a list of files with:
- `file_id`: Unique file identifier
- `filename`: Original filename
- `link`: URL to get file details
- Additional file metadata

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def list_files(
    request: Request,
    partition: str,
    limit: int | None = None,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    log = logger.bind(partition=partition)
    result = await app_service.list_files(
        partition=partition,
        allowed_partitions=[partition],
        limit=limit,
    )
    file_dicts = result.get("files", [])
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


@router.get(
    "/{partition}/file/{file_id}",
    description="""Get details and chunks for a specific file.

**Parameters:**
- `partition`: The partition name
- `file_id`: The unique file identifier

**Response:**
Returns file information including:
- `metadata`: File metadata (filename, size, timestamps, etc.)
- `documents`: Array of document chunks with links to detailed views

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def get_file(
    request: Request,
    partition: str,
    file_id: str,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    try:
        # REST route only needs chunk IDs (for link generation), not content —
        # pass limit=-1 to retrieve all chunks in one call without pagination.
        chunks_result = await app_service.get_file_chunks(
            partition=partition,
            file_id=file_id,
            allowed_partitions=[partition],
            limit=-1,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )

    documents = [
        {"link": str(request.url_for("get_extract", extract_id=doc["chunk_id"]))} for doc in chunks_result["chunks"]
    ]
    metadata = chunks_result["chunks"][0]["metadata"] if chunks_result["chunks"] else {}

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"metadata": metadata, "documents": documents},
    )


@router.get(
    "/{partition}/chunks",
    description="""List all document chunks in a partition.

**Parameters:**
- `partition`: The partition name
- `include_embedding`: Include vector embeddings in response (default: true)

**Response:**
Returns all chunks with:
- `content`: Chunk text content
- `metadata`: Chunk metadata (file_id, page, timestamps, etc.)
- `link`: URL to get chunk details
- `embedding`: Vector embedding (if include_embedding=true)

**Permissions:**
- Requires partition viewer role or higher

**Note:** This can return large amounts of data for partitions with many documents.
""",
)
async def list_all_chunks(
    request: Request,
    partition: str,
    include_embedding: bool = True,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    result = await app_service.list_partition_chunks(
        vectordb=vectordb,
        partition=partition,
        include_embedding=include_embedding,
    )
    chunks = result["chunks"]
    chunks = [
        {
            "link": str(request.url_for("get_extract", extract_id=chunk.metadata["_id"])),
            "content": chunk.page_content,
            "metadata": chunk.metadata,
        }
        for chunk in chunks
    ]
    return JSONResponse(status_code=status.HTTP_200_OK, content={"chunks": chunks})


@router.post(
    "/{partition}",
    description="""Create a new partition.

**Parameters:**
- `partition`: The partition name (must be unique)

**Behavior:**
- Creates an empty partition
- Automatically assigns you as the partition owner
- Sets up necessary indexes and schemas

**Response:**
Returns 201 Created on successful creation.

**Error:**
Returns 409 Conflict if partition already exists.
""",
)
async def create_partition(request: Request, partition: str, vectordb=Depends(get_vectordb)):
    if await vectordb.partition_exists.remote(partition):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Partition '{partition}' already exists.",
        )
    user_id = request.state.user["id"]
    await app_service.create_partition(vectordb=vectordb, partition=partition, user_id=user_id)
    return Response(status_code=status.HTTP_201_CREATED)


@router.get(
    "/{partition}/users",
    description="""List all users with access to a partition.

**Parameters:**
- `partition`: The partition name

**Response:**
Returns list of partition members with:
- `user_id`: User identifier
- `role`: User's role (owner, editor, or viewer)
- Additional user details

**Permissions:**
- Requires partition owner role

**Role Types:**
- `owner`: Full control (delete partition, manage users)
- `editor`: Can add/edit/delete files
- `viewer`: Read-only access
""",
)
async def list_partition_users(
    partition: str,
    vectordb=Depends(get_vectordb),
    partition_owner=Depends(require_partition_owner),
):
    """
    List all users who are members of the given partition.
    """
    log = logger.bind(partition=partition)

    result = await app_service.list_partition_users(vectordb=vectordb, partition=partition)
    members = result["members"]

    log.debug("Returned list of partition members.", member_count=len(members))
    return JSONResponse(status_code=status.HTTP_200_OK, content={"members": members})


@router.post(
    "/{partition}/users",
    description="""Add a user to a partition with a specific role.

**Parameters:**
- `partition`: The partition name
- `user_id`: User identifier (form data)
- `role`: User's role - owner, editor, or viewer (form data, default: viewer)

**Permissions:**
- Requires partition owner role

**Role Capabilities:**
- `owner`: Full control including user management
- `editor`: Can add, edit, and delete files
- `viewer`: Read-only access to partition contents

**Response:**
Returns 201 Created on successful addition.
""",
)
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

    await app_service.add_partition_user(vectordb=vectordb, partition=partition, user_id=user_id, role=role)

    log.debug("User added to partition successfully")
    return Response(status_code=status.HTTP_201_CREATED)


@router.delete(
    "/{partition}/users/{user_id}",
    description="""Remove a user from a partition.

**Parameters:**
- `partition`: The partition name
- `user_id`: User identifier to remove

**Permissions:**
- Requires partition owner role

**Behavior:**
- Removes user's access to the partition
- User can no longer view or edit partition contents
- Does not delete the user account itself

**Response:**
Returns 204 No Content on successful removal.
""",
)
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

    await app_service.remove_partition_user(vectordb=vectordb, partition=partition, user_id=user_id)

    log.debug("User removed from partition successfully")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{partition}/users/{user_id}",
    description="""Update a user's role in a partition.

**Parameters:**
- `partition`: The partition name
- `user_id`: User identifier
- `role`: New role - owner, editor, or viewer (form data)

**Permissions:**
- Requires partition owner role

**Role Types:**
- `owner`: Full control (manage users, delete partition)
- `editor`: Can add, edit, and delete files
- `viewer`: Read-only access

**Response:**
Returns 200 OK on successful update.
""",
)
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

    await app_service.update_partition_user_role(vectordb=vectordb, partition=partition, user_id=user_id, role=role)

    log.debug("User role updated successfully")
    return Response(status_code=status.HTTP_200_OK)
