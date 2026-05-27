"""User management routes — thin HTTP layer over :class:`UserService`.

Phase 8A.2: business logic (validation, default-quota rule, existence /
not-found semantics, repo delegation) moved to
``services.orchestrators.user_service.UserService``. This module keeps
HTTP transport only: request-scoped authorization (the shared FastAPI
``Depends`` wrappers in ``routers/utils.py``, retired in a later phase),
the two ``id == 1`` guard rules whose exact ``{"detail": ...}`` body the
legacy endpoints returned via ``HTTPException``, and response shaping.

``GET /users/info`` stays here unchanged — it computes effective quota
from the ``TaskStateManager`` Ray actor, which orchestrators must not
touch (Phase 8H); it will move to a service once the queue is de-Ray'd.
"""

from di.providers import get_user_service
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from models.user import UserCreate, UserPublic, UserUpdate
from services.orchestrators.user_service import UserService
from utils.logger import get_logger

from .utils import current_user, require_admin, require_admin_or_self

logger = get_logger()
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
async def list_users(
    admin_user=Depends(require_admin),
    service: UserService = Depends(get_user_service),
):
    users = await service.list_users()
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
async def get_current_user_info(
    user=Depends(current_user),
    service: UserService = Depends(get_user_service),
):
    """Get current authenticated user info"""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=await service.get_current_user_info(user),
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
    * `<0`: Unlimited
    * `>=0`: Specific limit for this user. The value can exceed the global default quota.

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
    body: UserCreate,
    admin_user=Depends(require_admin),
    service: UserService = Depends(get_user_service),
):
    """Create a new user and generate a token."""
    user = await service.create_user(body)
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
async def get_user(
    user_id: int,
    admin_user=Depends(require_admin),
    service: UserService = Depends(get_user_service),
):
    """Get details of a specific user (without exposing token)."""
    user = await service.get_user(user_id)
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
async def delete_user(
    user_id: int,
    admin_user=Depends(require_admin),
    service: UserService = Depends(get_user_service),
):
    """Delete a user."""
    if user_id == 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete default admin user.",
        )
    await service.delete_user(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{user_id}/regenerate_token",
    description="""Regenerate a user's authentication token.

**Parameters:**
- `user_id`: User identifier

**Permissions:**
- Requires admin role, or the caller must be regenerating their own token.

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
async def regenerate_user_token(
    user_id: int,
    _auth=Depends(require_admin_or_self),
    service: UserService = Depends(get_user_service),
):
    """Regenerate a user's token."""
    user = await service.regenerate_token(user_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=user)


@router.patch(
    "/{user_id}",
    description="""Update a user's profile information.

**Parameters:**
- `user_id`: User identifier
- `display_name`: New display name (optional)
- `external_user_id`: New external system user ID (optional)
- `is_admin`: Grant or revoke admin privileges (optional)
- `file_quota`: File quota override (optional)
    * `None` or omitted: field is not changed
    * `< 0`: Unlimited
    * `>= 0`: Specific file limit for this user

Only fields explicitly provided in the request body are updated.

**Permissions:**
- Requires admin role

**Response:**
Returns updated user details including:
- `id`: User identifier
- `display_name`: Updated display name
- `external_user_id`: Updated external ID
- `is_admin`: Updated admin status
- `created_at`: Account creation timestamp
- `file_quota`: File quota setting (`null` = use global default, `< 0` = unlimited)
- `file_count`: Number of indexed files
""",
)
async def update_user(
    user_id: int,
    body: UserUpdate,
    admin_user=Depends(require_admin),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Update a user's profile fields."""
    # Only block if is_admin was explicitly set to False in the request.
    if user_id == 1 and "is_admin" in body.model_fields_set and body.is_admin is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot revoke admin privileges from the default admin user.",
        )
    return await service.update_user(user_id, body)
