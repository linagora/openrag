"""Metadata-only table definitions for the Postgres catalog.

Defines the same 7 tables that ``components/indexer/vectordb/models.py`` declares
with the SQLAlchemy ORM, but as :class:`sqlalchemy.Table` objects bound to a
single :class:`sqlalchemy.MetaData`. The new persistence layer talks to
Postgres through ``asyncpg`` with raw SQL; this module exists so that Alembic's
autogenerate has a metadata target to diff against.

Column types, defaults, foreign keys, unique constraints, check constraints
and indexes must stay identical to the ORM models — Alembic will treat any
divergence as a pending schema change.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()


model_endpoints = Table(
    "model_endpoints",
    metadata,
    Column("name", String, primary_key=True),
    Column("model_type", String, primary_key=True),
    Column("endpoint", String, nullable=False),
    Column("model_name", String, nullable=True),
    Column("batch_size", Integer, server_default="32", nullable=False),
    Column("timeout", Float, server_default="30.0", nullable=False),
    Column("extra", JSONB, server_default=text("'{}'::jsonb"), nullable=False),
    Column("is_default", Boolean, server_default="false", nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    CheckConstraint(
        "model_type IN ('embedder','reranker','llm','vlm')",
        name="ck_model_endpoint_type",
    ),
)


pipeline_presets = Table(
    "pipeline_presets",
    metadata,
    Column("name", String, primary_key=True),
    Column("preset_type", String, primary_key=True),
    Column("config", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    CheckConstraint(
        "preset_type IN ('indexation','retrieval')",
        name="ck_pipeline_preset_type",
    ),
)


# The 8 canonical prompt types — kept in sync with core.models.prompt.PromptType.
# Used by the CHECK constraint on the prompts table so a junk type can never be
# stored. (Mirrors the ck_model_endpoint_type / ck_pipeline_preset_type pattern.)
_PROMPT_TYPE_VALUES = (
    "sys_prompt",
    "query_contextualizer",
    "chunk_contextualizer",
    "image_captioning",
    "hyde",
    "multi_query",
    "spoken_style_answer",
    "topic_tagger",
)
_PROMPT_TYPE_IN = "prompt_type IN (" + ",".join(f"'{v}'" for v in _PROMPT_TYPE_VALUES) + ")"


prompts = Table(
    "prompts",
    metadata,
    # String (not native UUID) so the column round-trips 1:1 with the
    # ``Prompt.id: str`` domain model without asyncpg UUID<->str coercion.
    Column("id", String, primary_key=True),
    Column("prompt_type", String, nullable=False),
    Column("name", String, server_default=text("''"), nullable=False),
    Column("content", String, nullable=False),
    Column("is_default", Boolean, server_default="false", nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    CheckConstraint(_PROMPT_TYPE_IN, name="ck_prompt_type"),
    Index("ix_prompts_type", "prompt_type"),
    # At most one global default per type — the DB-level guardrail behind
    # PromptService.set_default's clear-then-set (same shape as the model
    # endpoint default invariant, enforced there in application code).
    Index(
        "uix_prompts_default_per_type",
        "prompt_type",
        unique=True,
        postgresql_where=text("is_default = true"),
    ),
)


partitions = Table(
    "partitions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("partition", String, unique=True, nullable=False, index=True),
    Column("created_at", DateTime, default=datetime.now, nullable=False, index=True),
    Column("description", String, server_default=text("''"), nullable=False),
    Column("embedder", String, server_default=text("'default'"), nullable=False),
    Column("indexation_preset", String, server_default=text("'default'"), nullable=False),
    Column("retrieval_preset", String, server_default=text("'default'"), nullable=False),
    Column("dimension", Integer, server_default="1024", nullable=False),
    Column("collection_name", String, nullable=True),
    Column("chat_history_depth", Integer, server_default="0", nullable=False),
    Column("chat_llm", String, nullable=True),
    # {prompt_type: library_prompt_name} for generation prompts (sys_prompt,
    # spoken_style_answer). Like chat_llm, generation config lives on the
    # partition; indexation/retrieval prompts are named on their presets instead.
    Column(
        "generation_prompt_names",
        JSONB,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
)


files = Table(
    "files",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("file_id", String, nullable=False, index=True),
    Column(
        "partition_name",
        String,
        ForeignKey("partitions.partition"),
        nullable=False,
        index=True,
    ),
    Column("file_metadata", JSON, nullable=True, default=dict),
    Column("indexation_config", JSONB, nullable=True),
    Column(
        "created_by",
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    ),
    Column("relationship_id", String, nullable=True, index=True),
    Column("parent_id", String, nullable=True, index=True),
    Column("content_sha256", String(64), nullable=True),
    Column(
        "indexed_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    UniqueConstraint("file_id", "partition_name", name="uix_file_id_partition"),
    Index("ix_partition_file", "partition_name", "file_id"),
    Index("ix_relationship_partition", "relationship_id", "partition_name"),
    Index("ix_parent_partition", "parent_id", "partition_name"),
    Index(
        "uix_files_partition_content_sha256",
        "partition_name",
        "content_sha256",
        unique=True,
        postgresql_where=text("content_sha256 IS NOT NULL"),
    ),
)


file_content_claims = Table(
    "file_content_claims",
    metadata,
    Column(
        "partition_name",
        String,
        ForeignKey("partitions.partition", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("content_sha256", String(64), primary_key=True),
    Column("file_id", String, nullable=False),
    Column(
        "claim_token",
        String,
        server_default=text("md5(random()::text || clock_timestamp()::text)"),
        nullable=False,
    ),
    Column(
        "expires_at",
        DateTime(timezone=True),
        server_default=text("now() + interval '24 hours'"),
        nullable=False,
    ),
)


topic_tags = Table(
    "topic_tags",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("document_id", String, nullable=False),
    Column("partition", String, nullable=False, index=True),
    Column("tag", String, nullable=False),
    Column("normalized_tag", String, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    ForeignKeyConstraint(
        ["document_id", "partition"],
        ["files.file_id", "files.partition_name"],
        ondelete="CASCADE",
        name="fk_topic_tags_file",
    ),
    UniqueConstraint("document_id", "partition", "normalized_tag", name="uix_topic_tags_document_partition_tag"),
    Index("ix_topic_tags_document_id", "document_id"),
    Index("ix_topic_tags_partition_tag", "partition", "normalized_tag"),
)


users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("external_user_id", String, unique=True, nullable=True, index=True),
    Column("display_name", String, nullable=True),
    Column("email", String, unique=True, nullable=True, index=True),
    Column("token", String, unique=True, nullable=True, index=True),
    Column("is_admin", Boolean, default=False, nullable=False),
    Column("created_at", DateTime, default=datetime.now, nullable=False),
    Column("file_quota", Integer, nullable=True, default=None),
    Column("file_count", Integer, nullable=False, default=0),
)


oidc_sessions = Table(
    "oidc_sessions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "session_token_hash",
        String(64),
        unique=True,
        nullable=False,
        index=True,
    ),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("sid", String, nullable=True, index=True),
    Column("sub", String, nullable=False),
    Column("id_token_encrypted", LargeBinary, nullable=True),
    Column("access_token_encrypted", LargeBinary, nullable=True),
    Column("refresh_token_encrypted", LargeBinary, nullable=True),
    Column("access_token_expires_at", DateTime, nullable=False),
    Column("session_expires_at", DateTime, nullable=False),
    Column("created_at", DateTime, default=datetime.now, nullable=False),
    Column("last_refresh_at", DateTime, nullable=True),
    Column("revoked_at", DateTime, nullable=True),
    Index("ix_oidc_sessions_user_sub", "user_id", "sub"),
)


partition_memberships = Table(
    "partition_memberships",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "partition_name",
        String,
        ForeignKey("partitions.partition", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("role", String, nullable=False),
    Column("added_at", DateTime, default=datetime.now, nullable=False),
    UniqueConstraint("partition_name", "user_id", name="uix_partition_user"),
    CheckConstraint(
        "role IN ('owner','editor','viewer')",
        name="ck_membership_role",
    ),
    Index("ix_user_partition", "user_id", "partition_name"),
)


workspaces = Table(
    "workspaces",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("workspace_id", String, unique=True, nullable=False, index=True),
    Column(
        "partition_name",
        String,
        ForeignKey("partitions.partition", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column(
        "created_by",
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("display_name", String, nullable=True),
    Column("created_at", DateTime, default=datetime.now),
)


workspace_files = Table(
    "workspace_files",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "workspace_id",
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column(
        "file_id",
        Integer,
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    UniqueConstraint("workspace_id", "file_id", name="uix_workspace_file"),
)


__all__ = [
    "metadata",
    "model_endpoints",
    "pipeline_presets",
    "prompts",
    "topic_tags",
    "partitions",
    "files",
    "users",
    "oidc_sessions",
    "partition_memberships",
    "workspaces",
    "workspace_files",
]
