from pydantic import BaseModel, ConfigDict, Field


class UserBase(BaseModel):
    display_name: str | None = None
    external_user_id: str | None = None
    is_admin: bool = False
    file_quota: int | None = Field(default=10)


class UserCreate(UserBase):
    model_config = ConfigDict(extra="allow")


class UserUpdate(UserBase):
    model_config = ConfigDict(extra="allow")


class UserPublic(UserBase):
    id: int
    created_at: str | None
    file_quota: int | None = None
    file_count: int | None = None
