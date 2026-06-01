from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_vectordb
from utils.logger import get_logger

from .audit_utils import summarize_audit_run
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
    if len(partitions) == 1 and partitions[0]["partition"] == "all":
        partitions = await vectordb.list_partitions.remote()
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
    await vectordb.delete_partition.remote(partition)
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
    file_obj_l = await vectordb.list_partition_files.remote(partition=partition, limit=limit)
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
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )
    results = await vectordb.get_file_chunks.remote(partition=partition, file_id=file_id, include_id=True, limit=limit)

    documents = [{"link": str(request.url_for("get_extract", extract_id=doc.metadata["_id"]))} for doc in results]

    metadata = {k: v for k, v in results[0].metadata.items() if k != "_id"} if results else {}

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
    chunks = await vectordb.list_all_chunk.remote(partition=partition, include_embedding=include_embedding)
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
    await vectordb.create_partition.remote(partition=partition, user_id=user_id)
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

    members = await vectordb.list_partition_members.remote(partition=partition)

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

    await vectordb.add_partition_member.remote(partition=partition, user_id=user_id, role=role)

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

    await vectordb.remove_partition_member.remote(partition=partition, user_id=user_id)

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

    await vectordb.update_partition_member_role.remote(partition=partition, user_id=user_id, new_role=role)

    log.debug("User role updated successfully")
    return Response(status_code=status.HTTP_200_OK)


# RAG audit endpoints


@router.get(
    "/{partition}/audit/summary",
    description="""Get a compact latest completed RAG audit summary for a partition.

**Parameters:**
- `partition`: The partition name

**Response:**
Returns overall score, grade, counts, timestamps, and each audit axis with score
and key metrics. Detailed chart data and audit logs are omitted.

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def get_latest_partition_audit_summary(
    partition: str,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    run = await vectordb.get_latest_audit_run.remote(partition=partition, status="completed")
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No completed audit run found for partition '{partition}'",
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=summarize_audit_run(run))


@router.get(
    "/{partition}/audit",
    description="""Get the latest completed RAG audit result for a partition.

**Parameters:**
- `partition`: The partition name

**Response:**
Returns the latest completed audit run with overall score, grade, counts, timestamps,
and sanitized per-axis result payload.

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def get_latest_partition_audit(
    partition: str,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    run = await vectordb.get_latest_audit_run.remote(partition=partition, status="completed")
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No completed audit run found for partition '{partition}'",
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=run)


@router.get(
    "/{partition}/audit/runs",
    description="""List RAG audit runs for a partition.

**Parameters:**
- `partition`: The partition name
- `limit`: Maximum number of runs to return (default: 20, max: 100)
- `status`: Optional status filter: running, completed, failed, or skipped

**Response:**
Returns audit run summaries without the full result JSON payload.

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def list_partition_audit_runs(
    partition: str,
    limit: int = 20,
    status_filter: str | None = Query(None, alias="status"),
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be between 1 and 100")
    if status_filter and status_filter not in {"running", "completed", "failed", "skipped"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audit run status")
    runs = await vectordb.list_audit_runs.remote(partition=partition, limit=limit, status=status_filter)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"runs": runs})


@router.get(
    "/{partition}/audit/runs/{run_id}",
    description="""Get a specific RAG audit run for a partition.

**Parameters:**
- `partition`: The partition name
- `run_id`: Audit run identifier

**Response:**
Returns a full audit run, including sanitized result JSON when available.

**Permissions:**
- Requires partition viewer role or higher
""",
)
async def get_partition_audit_run(
    partition: str,
    run_id: str,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    run = await vectordb.get_audit_run.remote(partition=partition, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit run '{run_id}' not found for partition '{partition}'",
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=run)


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
    request: Request,
    partition: str,
    relationship_id: str,
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    log = logger.bind(partition=partition, relationship_id=relationship_id)
    files = await vectordb.get_files_by_relationship.remote(partition=partition, relationship_id=relationship_id)
    log.debug("Listed related files", file_count=len(files))
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"files": files},
    )


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
    request: Request,
    partition: str,
    file_id: str,
    max_ancestor_depth: int | None = None,  # Optional limit on ancestor depth
    vectordb=Depends(get_vectordb),
    partition_viewer=Depends(require_partition_viewer),
):
    log = logger.bind(partition=partition, file_id=file_id)

    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )

    ancestors = await vectordb.get_file_ancestors.remote(
        partition=partition, file_id=file_id, max_ancestor_depth=max_ancestor_depth
    )
    log.debug("Listed file ancestors", ancestor_count=len(ancestors))
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"ancestors": ancestors},
    )
