from pydantic import BaseModel


class LoginResponse(BaseModel):
    detail: str


class CurrentUserResponse(BaseModel):
    user_id: int | None
    email: str | None = None
    auth_method: str
    session_expires_at: str | None = None


class BackchannelLogoutErrorResponse(BaseModel):
    error: str
    error_description: str | None = None
