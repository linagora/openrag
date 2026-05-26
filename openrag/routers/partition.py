"""Partition routes — thin HTTP layer over :class:`PartitionService`.

Phase 8B.1: partition CRUD, membership, file/chunk reads and the
relationship queries moved to
``services.orchestrators.partition_service.PartitionService``. This
module keeps HTTP transport only: request-scoped authorization (the
shared ``Depends`` wrappers in ``routers/utils.py``), ``request.url_for``
link building, and the conflict / not-found guards whose exact
non-bracketed ``{"detail": ...}`` body the legacy endpoints returned via
``HTTPException``.
"""

from typing import Literal
from urllib.parse import quote

from di.providers import get_partition_service
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from services.orchestrators.partition_service import PartitionService
from utils.logger import get_logger

from .utils import (
    partitions_with_details,
    require_partition_owner,
    require_partition_viewer,
)

logger = get_logger()
router = APIRouter()

RoleType = Literal["viewer", "editor", "owner"]


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
    partitions=Depends(partitions_with_details),
    service: PartitionService = Depends(get_partition_service),
):
    if len(partitions) == 1 and partitions[0]["partition"] == "all":
        partitions = await service.list_partitions()
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
    partition_owner=Depends(require_partition_owner),
    service: PartitionService = Depends(get_partition_service),
):
    await service.delete_partition(partition)
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
    partition_viewer=Depends(require_partition_viewer),
    service: PartitionService = Depends(get_partition_service),
):
    file_dicts = await service.list_files(partition, limit)

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
- `limit`: Maximum number of chunks to return (default: 2000)

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
    limit: int = 2000,
    partition_viewer=Depends(require_partition_viewer),
    service: PartitionService = Depends(get_partition_service),
):
    if not await service.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )
    rows = await service.get_file_chunks(partition=partition, file_id=file_id, limit=limit)
    documents = [{"link": str(request.url_for("get_extract", extract_id=row["_id"]))} for row in rows]
    metadata = {k: v for k, v in rows[0].items() if k != "_id"} if rows else {}

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
    partition_viewer=Depends(require_partition_viewer),
    service: PartitionService = Depends(get_partition_service),
):
    items = await service.list_all_chunks(partition=partition, include_embedding=include_embedding)
    chunks = [
        {
            "link": str(request.url_for("get_extract", extract_id=it["metadata"]["_id"])),
            "content": it["content"],
            "metadata": it["metadata"],
        }
        for it in items
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
async def create_partition(
    request: Request,
    partition: str,
    service: PartitionService = Depends(get_partition_service),
):
    if await service.partition_exists(partition):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Partition '{partition}' already exists.",
        )
    user_id = request.state.user["id"]
    await service.create_partition(partition=partition, user_id=user_id)
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
    partition_owner=Depends(require_partition_owner),
    service: PartitionService = Depends(get_partition_service),
):
    """List all users who are members of the given partition."""
    members = await service.list_members(partition=partition)
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
    partition_owner=Depends(require_partition_owner),
    service: PartitionService = Depends(get_partition_service),
):
    """Add a user as a member of the given partition."""
    await service.add_member(partition=partition, user_id=user_id, role=role)
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
    partition_owner=Depends(require_partition_owner),
    service: PartitionService = Depends(get_partition_service),
):
    """Remove a user from the given partition."""
    await service.remove_member(partition=partition, user_id=user_id)
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
    partition_owner=Depends(require_partition_owner),
    service: PartitionService = Depends(get_partition_service),
):
    """Update a user's role in the given partition."""
    await service.update_role(partition=partition, user_id=user_id, new_role=role)
    return Response(status_code=status.HTTP_200_OK)


# Document relationship endpoints


@router.get(
    "/{partition}/relationships/{relationship_id:path}",
    description="""Get all files in a relationship group.

**Parameters:**
- `partition`: The partition name
- `relationship_id`: The relationship group identifier (e.g., email thread ID, folder path)

**Response:**
Returns all files that share the same relationship_id:
- `files`: List of file objects with metadata

**Use Cases:**
- Get all emails in a thread
- Get all documents in a folder
- Get all related documents in a group

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def get_related_files(
    partition: str,
    relationship_id: str,
    partition_viewer=Depends(require_partition_viewer),
    service: PartitionService = Depends(get_partition_service),
):
    files = await service.get_related_files(partition=partition, relationship_id=relationship_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"files": files})


@router.get(
    "/{partition}/file/{file_id}/ancestors",
    description="""Get the ancestor path for a file.

**Parameters:**
- `partition`: The partition name
- `file_id`: The file identifier (can be any node in a hierarchy)
- `max_ancestor_depth`: Maximum depth of ancestor files to include. None means unlimited. (default: None)

**Response:**
Returns the complete path from root to the specified file:
- `ancestors`: Ordered list of file objects (root first, target file last)

**Use Cases:**
- Get the email thread path from original email to a reply
- Get the folder hierarchy path to a file
- Reconstruct conversation history

**Note:**
This returns only the direct ancestor path, not sibling branches.
For email threads with parallel branches, each branch has its own ancestor path.

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def get_file_ancestors(
    partition: str,
    file_id: str,
    max_ancestor_depth: int | None = None,
    partition_viewer=Depends(require_partition_viewer),
    service: PartitionService = Depends(get_partition_service),
):
    if not await service.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )
    ancestors = await service.get_file_ancestors(
        partition=partition, file_id=file_id, max_ancestor_depth=max_ancestor_depth
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"ancestors": ancestors})
