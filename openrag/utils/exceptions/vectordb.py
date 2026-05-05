# Re-export from canonical location for backward compatibility.
# New code should import from `core.utils.exceptions` directly.
from core.utils.exceptions import (  # noqa: F401
    UnexpectedVDBError,
    VDBConnectionError,
    VDBCreateOrLoadCollectionError,
    VDBDeleteError,
    VDBError,
    VDBFileIDAlreadyExistsError,
    VDBFileNotFoundError,
    VDBInsertError,
    VDBMembershipNotFound,
    VDBPartitionNotFound,
    VDBSchemaMigrationRequiredError,
    VDBSearchError,
    VDBUserNotFound,
)
