from pydantic import BaseModel, Field, field_validator


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = 5  # default to 5 if not provided


class FileMetadataSchema(BaseModel):
    """Schema for validating file upload metadata.

    Metadata is passed as JSON in the file upload form and contains
    optional file processing hints and domain filtering configuration.
    """
    mimetype: str | None = None
    domains: list[str] = Field(default_factory=list)

    # Allow additional fields for backward compatibility
    # (existing code may pass extra fields we don't validate)
    model_config = {"extra": "allow"}

    @field_validator('domains')
    @classmethod
    def validate_domains(cls, v):
        """Ensure domains is a list of non-empty strings."""
        if v:
            if not isinstance(v, list):
                raise ValueError("domains must be a list")
            for domain in v:
                if not isinstance(domain, str):
                    raise ValueError("All domains must be strings")
                if not domain.strip():
                    raise ValueError("Domain names cannot be empty")
        return v
