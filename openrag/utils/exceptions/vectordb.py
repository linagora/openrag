from .base import VDBError


class VDBConnectionError(VDBError):
    """Raised when connection to vector database fails."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_CONNECTION_ERROR",
            status_code=503,
            **kwargs,
        )


class VDBCreateOrLoadCollectionError(VDBError):
    """Raised when there's an issue with collection operations."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, code="VDB_COLLECTION_ERROR", status_code=422, **kwargs)


class VDBInsertError(VDBError):
    """Raised when data insertion fails."""

    def __init__(self, message: str, status_code: int = 422, **kwargs):
        super().__init__(message=message, code="VDB_INSERT_ERROR", status_code=status_code, **kwargs)


class VDBFileIDAlreadyExistsError(VDBError):
    """Raised when a file already exists in the vector database."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, code="VDB_FILE_ALREADY_EXISTS", status_code=409, **kwargs)


class VDBDeleteError(VDBError):
    """Raised when data deletion fails."""

    def __init__(
        self,
        message: str,
        status_code=422,
        **kwargs,
    ):
        super().__init__(message=message, code="VDB_DELETE_ERROR", status_code=status_code, **kwargs)


class VDBSearchError(VDBError):
    """Raised when vector search fails."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_SEARCH_ERROR",
            status_code=422,
            **kwargs,
        )


class VDBPartitionNotFound(VDBError):
    """Raised when a partition is not found in the vector database."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_PARTITION_NOT_FOUND",
            status_code=404,
            **kwargs,
        )


class VDBFileNotFoundError(VDBError):
    """Raised when a file is not found in the vector database."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_FILE_NOT_FOUND",
            status_code=404,
            **kwargs,
        )


class VDBUserNotFound(VDBError):
    """Raised when a user is not found in the vector database."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_USER_NOT_FOUND",
            status_code=404,
            **kwargs,
        )


class VDBMembershipNotFound(VDBError):
    """Raised when a partition membership is not found in the vector database."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_MEMBERSHIP_NOT_FOUND",
            status_code=404,
            **kwargs,
        )


class VDBSchemaMigrationRequiredError(VDBError):
    """Raised when the collection schema version does not match the expected version."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_SCHEMA_MIGRATION_REQUIRED",
            status_code=503,
            **kwargs,
        )


class UnexpectedVDBError(VDBError):
    """Raised for unexpected errors in vector database operations."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            code="VDB_UNEXPECTED_ERROR",
            status_code=500,
            **kwargs,
        )
