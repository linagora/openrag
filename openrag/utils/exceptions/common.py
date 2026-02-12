from .base import OpenRAGError


class FileStorageError(OpenRAGError):
    """Raised when file I/O operations fail (save, read, delete)."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, code="FILE_STORAGE_ERROR", status_code=500, **kwargs)


class RayActorError(OpenRAGError):
    """Raised when a Ray actor operation fails."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, code="RAY_ACTOR_ERROR", status_code=500, **kwargs)


class ToolExecutionError(OpenRAGError):
    """Raised when tool execution fails."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, code="TOOL_EXECUTION_ERROR", status_code=500, **kwargs)


class UnexpectedError(OpenRAGError):
    """Raised for unexpected errors that don't match any specific category."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, code="UNEXPECTED_ERROR", status_code=500, **kwargs)
