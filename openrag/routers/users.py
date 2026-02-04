from fastapi import APIRouter, Depends, Form, HTTPException, Response, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_task_state_manager, get_vectordb
from utils.logger import get_logger

from .utils import DEFAULT_FILE_QUOTA, current_user, require_admin

logger = get_logger()
task_state_manager = get_task_state_manager()
router = APIRouter()


@router.get(
    "/",
    description="""List all users in the system.

**Permissions:**
- Requires admin role

**Response:**
Returns list of all users with:
- `id`: User identifier
- `display_name`: User's display name
- `external_user_id`: External ID (if set)
- `is_admin`: Admin status
- `created_at`: Account creation timestamp

**Note:** User tokens are not included in the response.
""",
)
async def list_users(vectordb=Depends(get_vectordb), admin_user=Depends(require_admin)):
    users = await vectordb.list_users.remote()
    logger.debug("Returned list of users.", user_count=len(users))
    return JSONResponse(status_code=status.HTTP_200_OK, content={"users": users})


@router.get(
    "/info",
    description="""Get current authenticated user information.

**Authentication:**
Uses the token from the Authorization header.

**Response:**
Returns current user details including:
- `id`: User identifier
- `display_name`: User's display name
- `is_admin`: Admin status
- Additional user metadata
    - indexed_files: Number of files currently indexed for this user
    - pending_files: Number of files pending indexing for this user
    - total_files: Total of indexed + pending files
    - file_quota: Effective file quota for this user (considering admin status and user-specific quota)
        -1: Unlimited
        >0: Specific file limit

**Note:** No special permissions required - returns info for the authenticated user.
""",
)
async def get_current_user_info(user=Depends(current_user), vectordb=Depends(get_vectordb)):
    """Get current authenticated user info"""

    user_id = user.get("id")
    is_admin = user.get("is_admin", False)

    if is_admin:
        user_quota = float("inf")
    elif DEFAULT_FILE_QUOTA <= 0:
        user_quota = float("inf")
    else:
        user_quota = user.get("file_quota", None)
        if user_quota is None:
            user_quota = DEFAULT_FILE_QUOTA
        elif user_quota <= 0:
            user_quota = float("inf")

    indexed_count = user.get("file_count", 0)  # Get indexed file count from user info
    pending_count = await task_state_manager.get_user_pending_task_count.remote(
        user_id
    )  # Get pending task count from task manager

    total = indexed_count + pending_count

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            **user,
            "file_count": indexed_count,
            "pending_files": pending_count,
            "total_files": total,
            "file_quota": -1 if user_quota == float("inf") else user_quota,
        },
    )


@router.post(
    "/",
    description="""Create a new user account.

**Parameters:**
- `display_name`: User's display name (optional, form data)
- `external_user_id`: External system user ID (optional, form data)
- `is_admin`: Grant admin privileges (default: false, form data)
- `file_quota`: File quota for the user (optional, form data).
    * `None` or not provided: Use global default quota (`DEFAULT_FILE_QUOTA` env var)
    * `<=0`: Unlimited
    * `>0`: Specific limit for this user. The value can exceed the global default quota.

**Permissions:**
- Requires admin role

**Response:**
Returns created user including:
- `id`: New user identifier
- `display_name`: User's display name
- `token`: Authentication token (only shown once)
- `is_admin`: Admin status
- `created_at`: Account creation timestamp

**Note:** Store the token securely - it won't be shown again.
""",
)
async def create_user(
    display_name: str | None = Form(None),
    external_user_id: str | None = Form(None),
    is_admin: bool = Form(False),
    vectordb=Depends(get_vectordb),
    file_quota: int | None = Form(None),
    admin_user=Depends(require_admin),
):
    """
    Create a new user and generate a token.
    """
    user = await vectordb.create_user.remote(
        display_name=display_name,
        external_user_id=external_user_id,
        is_admin=is_admin,
        file_quota=file_quota,
    )
    logger.info("Created new user", user_id=user["id"])
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=user)


@router.get(
    "/{user_id}",
    description="""Get details for a specific user.

**Parameters:**
- `user_id`: User identifier

**Permissions:**
- Requires admin role

**Response:**
Returns user details including:
- `id`: User identifier
- `display_name`: User's display name
- `external_user_id`: External ID (if set)
- `is_admin`: Admin status
- `created_at`: Account creation timestamp

**Note:** User token is not included in the response.
""",
)
async def get_user(user_id: int, vectordb=Depends(get_vectordb), admin_user=Depends(require_admin)):
    """
    Get details of a specific user (without exposing token).
    """
    user = await vectordb.get_user.remote(user_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=user)


@router.delete(
    "/{user_id}",
    description="""Delete a user account.

**Parameters:**
- `user_id`: User identifier

**Permissions:**
- Requires admin role

**Behavior:**
- Permanently deletes the user account
- Removes user from all partitions
- Invalidates all user tokens

**Response:**
Returns 204 No Content on successful deletion.

**Note:** Cannot delete the default admin user (ID: 1).
""",
)
async def delete_user(user_id: int, vectordb=Depends(get_vectordb), admin_user=Depends(require_admin)):
    """
    Delete a user.
    """
    if user_id == 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete default admin user.",
        )
    await vectordb.delete_user.remote(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{user_id}/regenerate_token",
    description="""Regenerate a user's authentication token.

**Parameters:**
- `user_id`: User identifier

**Permissions:**
- Requires admin role (or user can regenerate their own token)

**Behavior:**
- Generates a new authentication token
- Invalidates the old token immediately
- Old token can no longer be used for authentication

**Response:**
Returns user details including the new token:
- `id`: User identifier
- `token`: New authentication token
- Additional user details

**Note:** Store the new token securely - the old token is now invalid.
""",
)
async def regenerate_user_token(user_id: int, vectordb=Depends(get_vectordb)):
    """
    Regenerate a user's token.
    """
    user = await vectordb.regenerate_user_token.remote(user_id)
    logger.info("Regenerated user token", user_id=user_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=user)


@router.patch(
    "/{user_id}/quota",
    description="""Update a user's file quota.

**Parameters:**
- `user_id`: User identifier
- `file_quota`: New file quota value (form data)
    * `None` or not provided: Use global default (`DEFAULT_FILE_QUOTA` env var)
    * `<=0`: Unlimited
    * `>0`: Specific limit for this user

**Permissions:**
- Requires admin role

**Response:**
Returns 200 OK with success message.

**Note:** Admins always have unlimited quota regardless of this setting.
""",
)
async def update_user_quota(
    user_id: int,
    file_quota: int | None = Form(None),
    vectordb=Depends(get_vectordb),
    admin_user=Depends(require_admin),
):
    """
    Update a user's file quota.
    """
    await vectordb.update_user_quota.remote(user_id, file_quota)

    logger.debug("Updated user quota", user_id=user_id, file_quota=file_quota)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": f"Quota for user {user_id} updated to {file_quota}"},
    )
