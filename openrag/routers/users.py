from components.app.service import OpenRAGApplicationService
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_vectordb
from utils.logger import get_logger

from .utils import require_admin

logger = get_logger()
router = APIRouter()
app_service = OpenRAGApplicationService()


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
    result = await app_service.list_users(vectordb=vectordb)
    users = result["users"]
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

**Note:** No special permissions required - returns info for the authenticated user.
""",
)
async def get_current_user(request: Request):
    """Get current authenticated user info"""
    return await app_service.get_current_user(user=request.state.user)


@router.post(
    "/",
    description="""Create a new user account.

**Parameters:**
- `display_name`: User's display name (optional, form data)
- `external_user_id`: External system user ID (optional, form data)
- `is_admin`: Grant admin privileges (default: false, form data)

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
    admin_user=Depends(require_admin),
):
    """
    Create a new user and generate a token.
    """
    user = await app_service.create_user(
        vectordb=vectordb,
        display_name=display_name,
        external_user_id=external_user_id,
        is_admin=is_admin,
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
    user = await app_service.get_user(vectordb=vectordb, user_id=user_id)
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
    await app_service.delete_user_account(vectordb=vectordb, user_id=user_id)
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
    user = await app_service.regenerate_user_token(vectordb=vectordb, user_id=user_id)
    logger.info("Regenerated user token", user_id=user_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=user)
