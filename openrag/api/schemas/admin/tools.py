"""Tool descriptor schema.

Pulled out of ``api/routers/admin/tools.py`` so the response DTO lives
under ``api/schemas/`` like the rest of the Phase 10E schemas.
"""

from pydantic import BaseModel


class ToolInfo(BaseModel):
    name: str
    description: str


__all__ = ["ToolInfo"]
