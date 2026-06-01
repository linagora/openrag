from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    declarative_base,
    relationship,
)

Base = declarative_base()


class File(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    file_id = Column(String, nullable=False, index=True)  # Added index for file_id lookups
    # Foreign key points directly to the partition string
    partition_name = Column(String, ForeignKey("partitions.partition"), nullable=False, index=True)  # Added index
    file_metadata = Column(JSON, nullable=True, default=dict)

    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Document relationship fields
    relationship_id = Column(
        String, nullable=True, index=True
    )  # Groups related documents (e.g., email thread ID, folder path)
    parent_id = Column(
        String, nullable=True, index=True
    )  # Hierarchical parent reference (e.g., parent email, parent folder)

    # relationship to the Partition object
    partition = relationship("Partition", back_populates="files")

    # Enforce uniqueness of (file_id, partition_name) - this also creates an index
    __table_args__ = (
        UniqueConstraint("file_id", "partition_name", name="uix_file_id_partition"),
        # Additional composite index for common query patterns (partition first for better selectivity)
        Index("ix_partition_file", "partition_name", "file_id"),
        # Indexes for relationship queries
        Index("ix_relationship_partition", "relationship_id", "partition_name"),
        Index("ix_parent_partition", "parent_id", "partition_name"),
    )

    def to_dict(self):
        metadata = self.file_metadata or {}
        d = {
            "partition": self.partition_name,
            "file_id": self.file_id,
            "relationship_id": self.relationship_id,
            "parent_id": self.parent_id,
            **metadata,
        }
        return d

    def __repr__(self):
        return f"<File(id={self.id}, file_id='{self.file_id}', partition='{self.partition_name}')>"


class Partition(Base):
    __tablename__ = "partitions"

    id = Column(Integer, primary_key=True)
    partition = Column(String, unique=True, nullable=False, index=True)  # Index already exists due to unique constraint
    created_at = Column(
        DateTime, default=datetime.now, nullable=False, index=True
    )  # Added index for time-based queries
    files = relationship("File", back_populates="partition", cascade="all, delete-orphan")
    memberships = relationship("PartitionMembership", back_populates="partition", cascade="all, delete-orphan")
    audit_runs = relationship("RagAuditRun", back_populates="partition", cascade="all, delete-orphan")
    workspaces = relationship(
        "Workspace",
        cascade="all, delete-orphan",
        backref="partition_ref",
        foreign_keys="Workspace.partition_name",
        primaryjoin="Partition.partition == Workspace.partition_name",
    )

    def to_dict(self):
        d = {
            "id": self.id,
            "partition": self.partition,
            "created_at": self.created_at.isoformat(),
        }
        return d

    def __repr__(self):
        return f"<Partition(key='{self.partition}', created_at='{self.created_at}')>"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    external_user_id = Column(String, unique=True, nullable=True, index=True)
    display_name = Column(String, nullable=True)
    email = Column(String, unique=True, nullable=True, index=True)
    token = Column(String, unique=True, nullable=True, index=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    file_quota = Column(Integer, nullable=True, default=None)
    file_count = Column(Integer, nullable=False, default=0)
    memberships = relationship("PartitionMembership", back_populates="user", cascade="all, delete-orphan")
    oidc_sessions = relationship("OIDCSession", back_populates="user", cascade="all, delete-orphan")


class OIDCSession(Base):
    __tablename__ = "oidc_sessions"

    id = Column(Integer, primary_key=True)
    session_token_hash = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sid = Column(String, nullable=True, index=True)  # OIDC session id claim (for back-channel logout)
    sub = Column(String, nullable=False)  # OIDC subject claim
    id_token_encrypted = Column(LargeBinary, nullable=True)
    access_token_encrypted = Column(LargeBinary, nullable=True)
    refresh_token_encrypted = Column(LargeBinary, nullable=True)
    access_token_expires_at = Column(DateTime, nullable=False)
    session_expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    last_refresh_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="oidc_sessions")

    __table_args__ = (Index("ix_oidc_sessions_user_sub", "user_id", "sub"),)


class PartitionMembership(Base):
    __tablename__ = "partition_memberships"

    id = Column(Integer, primary_key=True)
    partition_name = Column(
        String,
        ForeignKey("partitions.partition", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False)  # 'owner' | 'editor' | 'viewer'
    added_at = Column(DateTime, default=datetime.now, nullable=False)

    __table_args__ = (
        UniqueConstraint("partition_name", "user_id", name="uix_partition_user"),
        CheckConstraint("role IN ('owner','editor','viewer')", name="ck_membership_role"),
        Index("ix_user_partition", "user_id", "partition_name"),
    )

    partition = relationship("Partition", back_populates="memberships")
    user = relationship("User", back_populates="memberships")


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(String, unique=True, nullable=False, index=True)
    partition_name = Column(
        String,
        ForeignKey("partitions.partition", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    display_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    files = relationship("WorkspaceFile", cascade="all, delete-orphan", backref="workspace")


class WorkspaceFile(Base):
    __tablename__ = "workspace_files"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_id = Column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("workspace_id", "file_id", name="uix_workspace_file"),)


class RagAuditRun(Base):
    __tablename__ = "rag_audit_runs"

    id = Column(Integer, primary_key=True)
    run_id = Column(String, unique=True, nullable=False, index=True)
    partition_id = Column(Integer, nullable=True, index=True)
    partition_name = Column(
        String,
        ForeignKey("partitions.partition", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String, nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)
    document_count = Column(Integer, nullable=True)
    chunk_count = Column(Integer, nullable=True)
    overall_score = Column(Float, nullable=True)
    overall_grade = Column(String, nullable=True)
    config_json = Column(JSON, nullable=True, default=dict)
    result_json = Column(JSON, nullable=True, default=dict)
    error = Column(String, nullable=True)

    partition = relationship("Partition", back_populates="audit_runs")

    __table_args__ = (
        Index("ix_rag_audit_runs_partition_started", "partition_name", "started_at"),
        Index("ix_rag_audit_runs_partition_status", "partition_name", "status"),
    )

    def to_dict(self, include_result: bool = True):
        d = {
            "run_id": self.run_id,
            "partition": self.partition_name,
            "partition_name": self.partition_name,
            "partition_id": self.partition_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "overall_score": self.overall_score,
            "overall_grade": self.overall_grade,
            "config": self.config_json or {},
            "error": self.error,
        }
        if include_result:
            d["result"] = self.result_json or {}
        return d
