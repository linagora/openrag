"""Unified exception hierarchy for OpenRAG.

All exceptions inherit from OpenRAGError and carry a machine-readable
``code``, an HTTP ``status_code``, and an optional ``extra`` dict.

The hierarchy is organised by concern:

    OpenRAGError
    +-- ConfigError
    +-- RegistryError
    +-- PipelineError
    +-- AuthError
    |   +-- AuthenticationError          (401)
    +-- ValidationError                  (422)
    +-- NotFoundError                    (404)
    |   +-- DocumentNotFoundError
    |   +-- PartitionNotFoundError
    |   +-- UserNotFoundError
    +-- QuotaExceededError               (429)
    +-- ServiceUnavailableError          (503)
    |   +-- CircuitBreakerOpenError
    +-- InferenceError                   (503)
    |   +-- LLMParsingError              (502)
    |   +-- InferenceTimeoutError        (504)
    |   +-- InferenceConnectionError     (503)
    +-- StorageError                     (500)
    |   +-- MilvusError
    |   +-- PostgresError
    +-- EmbeddingError                   (500)
    |   +-- EmbeddingAPIError
    |   +-- EmbeddingResponseError       (422)
    |   +-- UnexpectedEmbeddingError
    +-- VDBError                         (500)
        +-- VDBConnectionError           (503)
        +-- VDBInsertError               (422)
        +-- VDBDeleteError               (422)
        +-- VDBSearchError               (422)
        +-- VDBFileIDAlreadyExistsError  (409)
        +-- VDBPartitionNotFound         (404)
        +-- VDBFileNotFoundError         (404)
        +-- VDBUserNotFound              (404)
        +-- VDBMembershipNotFound        (404)
        +-- VDBSchemaMigrationRequiredError (503)
        +-- VDBCreateOrLoadCollectionError  (422)
        +-- UnexpectedVDBError           (500)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class OpenRAGError(Exception):
    """Base class for all OpenRAG exceptions.

    Preserves the existing API: message, code, status_code, to_dict().
    """

    def __init__(
        self,
        message: str,
        code: str = "OPENRAG_ERROR",
        status_code: int = 500,
        **kwargs,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.extra = kwargs or {}
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict:
        return {
            "detail": f"[{self.code}]: {self.message}",
            "extra": self.extra,
        }


# ---------------------------------------------------------------------------
# Config & registry
# ---------------------------------------------------------------------------


class ConfigError(OpenRAGError):
    """Configuration-related errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="CONFIG_ERROR", status_code=500, **kwargs)


class RegistryError(OpenRAGError):
    """Registry lookup errors (unknown component name)."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="REGISTRY_ERROR", status_code=500, **kwargs)


class PipelineError(OpenRAGError):
    """Pipeline execution errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="PIPELINE_ERROR", status_code=500, **kwargs)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class AuthError(OpenRAGError):
    """Authentication / authorization errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="AUTH_ERROR", status_code=403, **kwargs)


class AuthenticationError(AuthError):
    """Missing or invalid credentials. Maps to HTTP 401."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, **kwargs)
        self.code = "AUTHENTICATION_ERROR"
        self.status_code = 401


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationError(OpenRAGError):
    """Input validation or business rule violation. Maps to HTTP 422 by default.

    Accepts a custom ``status_code`` so callers can preserve more specific
    semantics (e.g. 400 Bad Request for malformed input, 415 Unsupported
    Media Type for rejected file formats).
    """

    def __init__(self, message: str, *, status_code: int = 422, code: str = "VALIDATION_ERROR", **kwargs):
        super().__init__(message, code=code, status_code=status_code, **kwargs)


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------


class NotFoundError(OpenRAGError):
    """Requested resource not found. Maps to HTTP 404."""

    def __init__(self, message: str, code: str = "NOT_FOUND", **kwargs):
        super().__init__(message, code=code, status_code=404, **kwargs)


class DocumentNotFoundError(NotFoundError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="DOCUMENT_NOT_FOUND", **kwargs)


class PartitionNotFoundError(NotFoundError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="PARTITION_NOT_FOUND", **kwargs)


class UserNotFoundError(NotFoundError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="USER_NOT_FOUND", **kwargs)


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


class QuotaExceededError(OpenRAGError):
    """File quota exceeded. Maps to HTTP 429."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="QUOTA_EXCEEDED", status_code=429, **kwargs)


# ---------------------------------------------------------------------------
# Infrastructure — service availability
# ---------------------------------------------------------------------------


class ServiceUnavailableError(OpenRAGError):
    """External service unavailable after retry exhaustion. Maps to HTTP 503."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="SERVICE_UNAVAILABLE", status_code=503, **kwargs)


class CircuitBreakerOpenError(ServiceUnavailableError):
    """Circuit breaker is open. Maps to HTTP 503."""

    def __init__(self, service_type: str, **kwargs):
        self.service_type = service_type
        super().__init__(
            f"Circuit breaker open for {service_type} — service unavailable",
            **kwargs,
        )
        self.code = "CIRCUIT_BREAKER_OPEN"


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


class InferenceError(OpenRAGError):
    """Base for all inference service failures. Maps to HTTP 503."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="INFERENCE_ERROR", status_code=503, **kwargs)


class LLMParsingError(InferenceError):
    """LLM returned invalid JSON. Maps to HTTP 502."""

    def __init__(self, raw_response: str, parse_error: str | None = None, **kwargs):
        self.raw_response = raw_response[:500]
        self.parse_error = parse_error
        super().__init__(
            f"LLM returned invalid JSON: {self.raw_response[:100]}...",
            **kwargs,
        )
        self.code = "LLM_PARSING_ERROR"
        self.status_code = 502


class InferenceTimeoutError(InferenceError):
    """Inference request timed out. Maps to HTTP 504."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, **kwargs)
        self.code = "INFERENCE_TIMEOUT"
        self.status_code = 504


class InferenceConnectionError(InferenceError):
    """Cannot reach inference service. Maps to HTTP 503."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, **kwargs)
        self.code = "INFERENCE_CONNECTION_ERROR"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class StorageError(OpenRAGError):
    """Base for storage failures. Maps to HTTP 500."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="STORAGE_ERROR", status_code=500, **kwargs)


class MilvusError(StorageError):
    """Milvus-specific failures."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, **kwargs)
        self.code = "MILVUS_ERROR"


class PostgresError(StorageError):
    """Postgres-specific failures."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, **kwargs)
        self.code = "POSTGRES_ERROR"


# ---------------------------------------------------------------------------
# Embedding (preserves existing OpenRAG exception classes)
# ---------------------------------------------------------------------------


class EmbeddingError(OpenRAGError):
    """Base exception for all embedding-related errors."""

    def __init__(self, message: str, code: str = "EMBEDDING_ERROR", status_code: int = 500, **kwargs):
        super().__init__(message, code=code, status_code=status_code, **kwargs)


class EmbeddingAPIError(EmbeddingError):
    """API error with the embedding provider."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="EMBEDDING_API_ERROR", status_code=500, **kwargs)


class EmbeddingResponseError(EmbeddingError):
    """Invalid or unexpected response from embedding provider."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="EMBEDDING_RESPONSE_ERROR", status_code=422, **kwargs)


class UnexpectedEmbeddingError(EmbeddingError):
    """Unexpected error in embedding operations."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="EMBEDDING_UNEXPECTED_ERROR", status_code=500, **kwargs)


# ---------------------------------------------------------------------------
# Vector database (preserves existing OpenRAG exception classes)
# ---------------------------------------------------------------------------


class VDBError(OpenRAGError):
    """Base exception for all vector database-related errors."""

    def __init__(self, message: str, code: str = "VDB_ERROR", status_code: int = 500, **kwargs):
        super().__init__(message, code=code, status_code=status_code, **kwargs)


class VDBConnectionError(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_CONNECTION_ERROR", status_code=503, **kwargs)


class VDBCreateOrLoadCollectionError(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_COLLECTION_ERROR", status_code=422, **kwargs)


class VDBInsertError(VDBError):
    def __init__(self, message: str, status_code: int = 422, **kwargs):
        super().__init__(message, code="VDB_INSERT_ERROR", status_code=status_code, **kwargs)


class VDBFileIDAlreadyExistsError(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_FILE_ALREADY_EXISTS", status_code=409, **kwargs)


class VDBDeleteError(VDBError):
    def __init__(self, message: str, status_code: int = 422, **kwargs):
        super().__init__(message, code="VDB_DELETE_ERROR", status_code=status_code, **kwargs)


class VDBSearchError(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_SEARCH_ERROR", status_code=422, **kwargs)


class VDBPartitionNotFound(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_PARTITION_NOT_FOUND", status_code=404, **kwargs)


class VDBFileNotFoundError(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_FILE_NOT_FOUND", status_code=404, **kwargs)


class VDBUserNotFound(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_USER_NOT_FOUND", status_code=404, **kwargs)


class VDBMembershipNotFound(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_MEMBERSHIP_NOT_FOUND", status_code=404, **kwargs)


class VDBSchemaMigrationRequiredError(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_SCHEMA_MIGRATION_REQUIRED", status_code=503, **kwargs)


class UnexpectedVDBError(VDBError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VDB_UNEXPECTED_ERROR", status_code=500, **kwargs)
