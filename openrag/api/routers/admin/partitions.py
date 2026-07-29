"""Partition routes — thin HTTP layer over :class:`PartitionService`.

Partition CRUD, membership, file/chunk reads and the relationship
queries live in ``services.orchestrators.partition_service.PartitionService``.
This module keeps HTTP transport only: request-scoped authorization (the
shared ``Depends`` wrappers in :mod:`api.dependencies.auth`),
``request.url_for`` link building, and the conflict / not-found guards
whose exact non-bracketed ``{"detail": ...}`` body the endpoints return
via ``HTTPException``.
"""

import os
from typing import Literal
from urllib.parse import quote

from api.dependencies.auth import (
    partitions_with_details,
    require_partition_owner,
    require_partition_viewer,
)
from api.dependencies.files import validate_file_id
from api.schemas.admin.partition_schemas import PartitionDetailResponse, UpdatePartitionRequest
from core.utils.exceptions import ConfigError
from core.utils.logging import get_logger
from core.utils.partition_limits import max_partitions_for_user
from di.providers import get_partition_service
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

logger = get_logger()
router = APIRouter()

RoleType = Literal["viewer", "editor", "owner"]


def _quote_param_value(s: str) -> str:
    """Percent-encode a path parameter value for URL generation."""
    return quote(s, safe="")


def _require_service_method(service, method_name: str):
    """Return a service method or fail clearly when a phased method is absent."""
    method = getattr(service, method_name, None)
    if not callable(method):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"{method_name} is not available.",
        )
    return method


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
    request: Request,
    partitions=Depends(partitions_with_details),
    service=Depends(get_partition_service),
):
    """List partitions visible to the current user, with stored config + document_count."""
    # The ``all`` entry is the admin/SUPER_ADMIN_MODE sentinel from
    # partitions_with_details. Gate the all-expansion on the caller actually being
    # an admin, so a (legacy) partition literally named ``all`` owned by a regular
    # user cannot leak every partition. New ``all`` partitions are already rejected
    # at creation (_RESERVED_PARTITION_NAMES).
    is_admin = bool(request.state.user.get("is_admin"))
    summaries = await service.list_partition_summaries()
    if is_admin and len(partitions) == 1 and partitions[0]["partition"] == "all":
        result = list(summaries.values())
    else:
        result = []
        for p in partitions:
            name = p["partition"]
            row = dict(summaries.get(name) or {"partition": name, "document_count": 0})
            if p.get("role") is not None:
                row["role"] = p["role"]
            result.append(row)
    logger.debug("Returned list of existing partitions.", partition_count=len(result))
    return JSONResponse(status_code=status.HTTP_200_OK, content={"partitions": result})


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
    service=Depends(get_partition_service),
):
    """Delete a partition owned by the current user."""
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
    service=Depends(get_partition_service),
):
    """List files stored in a partition."""
    file_dicts = await service.list_files(partition, limit)

    def process_file(file_dict):
        """Add a canonical file-detail link to one file row."""
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
    limit: int = Query(default=2000, ge=0),
    file_id: str = Depends(validate_file_id),
    partition_viewer=Depends(require_partition_viewer),
    service=Depends(get_partition_service),
):
    """Return metadata and chunk links for one file in a partition."""
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
    description="""List document chunks in a partition.

**Parameters:**
- `partition`: The partition name
- `include_embedding`: Include vector embeddings in response (default: true)
- `file_id`: Restrict to a single file's chunks (filtered server-side; recommended
  for the document detail view to avoid loading the whole partition)
- `limit`: Maximum number of chunks to return (default: unbounded)

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
    file_id: str | None = None,
    limit: int | None = Query(default=None, ge=0),
    partition_viewer=Depends(require_partition_viewer),
    service=Depends(get_partition_service),
):
    """List chunks in a partition, optionally scoped to a single file."""
    items = await service.list_all_chunks(
        partition=partition,
        include_embedding=include_embedding,
        file_id=file_id,
        limit=limit,
    )
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
    service=Depends(get_partition_service),
):
    """Create a new partition owned by the current user."""
    if await service.partition_exists(partition):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Partition '{partition}' already exists.",
        )
    user = request.state.user
    user_id = user["id"]
    # Cap how many partitions a non-admin may own so an authenticated user can't
    # exhaust storage/metadata. None bypasses the cap (admins); a negative
    # MAX_PARTITIONS_PER_USER also disables it. The service raises a 403
    # (PARTITION_LIMIT_EXCEEDED) when the cap is reached.
    try:
        max_owned = max_partitions_for_user(user)
    except ConfigError as exc:
        logger.bind(
            max_partitions_per_user=os.environ.get("MAX_PARTITIONS_PER_USER"),
            user_id=user_id,
            is_admin=bool(user.get("is_admin")),
            partition=partition,
        ).error("Invalid MAX_PARTITIONS_PER_USER value")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exc.message,
        ) from exc
    await service.create_partition(partition=partition, user_id=user_id, max_owned=max_owned)
    return Response(status_code=status.HTTP_201_CREATED)


@router.patch(
    "/{partition}",
    response_model=PartitionDetailResponse,
    description="""Update Phase 14 preset assignments for a partition.

**Parameters:**
- `partition`: The partition name

**Body:**
Accepts partition config fields such as:
- `description`
- `embedder`
- `indexation_preset`
- `retrieval_preset`
- `chat_history_depth`
- `chat_llm` (must name a registered LLM endpoint — 422 otherwise; explicit `null` resets to the default LLM)

**Permissions:**
- Requires partition owner role

**Response:**
Returns the updated resolved partition configuration.
""",
)
async def update_partition_config(
    partition: str,
    body: UpdatePartitionRequest,
    partition_owner=Depends(require_partition_owner),
    service=Depends(get_partition_service),
):
    """Update Phase 14 preset references for a partition."""
    method = _require_service_method(service, "update_partition_config")
    return await method(
        partition=partition,
        **body.model_dump(exclude_unset=True),
    )


@router.get(
    "/{partition}/config",
    response_model=PartitionDetailResponse,
    description="""Return the resolved Phase 14 pipeline config for a partition.

**Parameters:**
- `partition`: The partition name

**Permissions:**
- Requires partition viewer role or higher

**Response:**
Returns partition metadata, preset references, and resolved indexation/retrieval pipeline configs.
""",
)
async def get_partition_config(
    partition: str,
    partition_viewer=Depends(require_partition_viewer),
    service=Depends(get_partition_service),
):
    """Return the resolved Phase 14 config for a partition."""
    method = _require_service_method(service, "get_partition_config")
    return await method(partition=partition)


@router.get(
    "/{partition}/users",
    description="""List all users with access to a partition.

**Parameters:**
- `partition`: The partition name

**Response:**
Returns list of partition members with:
- `user_id`: User identifier
- `display_name`: Human-readable name, when available
- `email`: Account email, when available
- `role`: User's role (owner, editor, or viewer)
- `added_at`: Membership creation time

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
    service=Depends(get_partition_service),
):
    """List all users who are members of the given partition."""
    members = await service.list_members_with_identities(partition=partition)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"members": members})


@router.get(
    "/{partition}/users/candidates",
    description="""List users who can be added to a partition.

**Parameters:**
- `partition`: The partition name
- `search`: Display-name prefix (at least 3 characters) or exact user ID
- `cursor`: Last user ID from the previous page
- `limit`: Page size (maximum 100)

**Response:**
Returns a bounded page of non-member users with their display name and email,
plus continuation metadata.

**Permissions:**
- Requires partition owner role
""",
)
async def list_partition_user_candidates(
    partition: str,
    search: str = Query(..., max_length=200),
    cursor: int | None = Query(default=None, ge=0, le=2_147_483_647),
    limit: int = Query(default=25, ge=1, le=100),
    partition_owner=Depends(require_partition_owner),
    service=Depends(get_partition_service),
):
    """List a searchable page of users who are not partition members."""
    page = await service.list_member_candidates(
        partition=partition,
        search=search,
        cursor=cursor,
        limit=limit,
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=page)


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
Returns 409 Conflict if the user is already a member; use the role endpoint to change an existing member.
""",
)
async def add_partition_user(
    partition: str,
    user_id: int = Form(...),
    role: RoleType = Form("viewer"),
    partition_owner=Depends(require_partition_owner),
    service=Depends(get_partition_service),
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
    service=Depends(get_partition_service),
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
    service=Depends(get_partition_service),
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
    service=Depends(get_partition_service),
):
    """Return files sharing a relationship identifier."""
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
    max_ancestor_depth: int | None = None,
    file_id: str = Depends(validate_file_id),
    partition_viewer=Depends(require_partition_viewer),
    service=Depends(get_partition_service),
):
    """Return the ancestor path for one file."""
    if not await service.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )
    ancestors = await service.get_file_ancestors(
        partition=partition, file_id=file_id, max_ancestor_depth=max_ancestor_depth
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"ancestors": ancestors})
