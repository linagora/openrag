import hashlib
import os
import secrets
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    declarative_base,
    relationship,
    sessionmaker,
)
from sqlalchemy_utils import (
    create_database,
    database_exists,
)
from utils.exceptions.vectordb import *
from utils.logger import get_logger

logger = get_logger()

Base = declarative_base()


class File(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    file_id = Column(String, nullable=False, index=True)  # Added index for file_id lookups
    # Foreign key points directly to the partition string
    partition_name = Column(String, ForeignKey("partitions.partition"), nullable=False, index=True)  # Added index
    file_metadata = Column(JSON, nullable=True, default={})

    # relationship to the Partition object
    partition = relationship("Partition", back_populates="files")

    # Enforce uniqueness of (file_id, partition_name) - this also creates an index
    __table_args__ = (
        UniqueConstraint("file_id", "partition_name", name="uix_file_id_partition"),
        # Additional composite index for common query patterns (partition first for better selectivity)
        Index("ix_partition_file", "partition_name", "file_id"),
    )

    def to_dict(self):
        metadata = self.file_metadata or {}
        d = {"partition": self.partition_name, "file_id": self.file_id, **metadata}
        return d

    def __repr__(self):
        return f"<File(id={self.id}, file_id='{self.file_id}', partition='{self.partition}')>"


# In the Partition model
class Partition(Base):
    __tablename__ = "partitions"

    id = Column(Integer, primary_key=True)
    partition = Column(String, unique=True, nullable=False, index=True)  # Index already exists due to unique constraint
    created_at = Column(
        DateTime, default=datetime.now, nullable=False, index=True
    )  # Added index for time-based queries
    files = relationship("File", back_populates="partition", cascade="all, delete-orphan")
    memberships = relationship("PartitionMembership", back_populates="partition", cascade="all, delete-orphan")

    def to_dict(self):
        d = {
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
    token = Column(String, unique=True, nullable=True, index=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    memberships = relationship("PartitionMembership", back_populates="user", cascade="all, delete-orphan")


class FileDomain(Base):
    __tablename__ = "file_domains"

    id = Column(Integer, primary_key=True)
    file_id = Column(String, nullable=False)
    partition_name = Column(
        String,
        ForeignKey("partitions.partition", ondelete="CASCADE"),
        nullable=False,
    )
    domain = Column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("file_id", "partition_name", "domain", name="uix_file_domain"),
        Index("ix_partition_domain", "partition_name", "domain"),
    )


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


class PartitionFileManager:
    def __init__(self, database_url: str, logger=logger):
        try:
            self.engine = create_engine(database_url)
            if not database_exists(database_url):
                create_database(database_url)

            Base.metadata.create_all(self.engine)
            self.logger = logger
            self.Session = sessionmaker(bind=self.engine)
            AUTH_TOKEN = os.getenv("AUTH_TOKEN")
            self._ensure_admin_user(AUTH_TOKEN)

        except Exception as e:
            raise VDBConnectionError(
                "An unexpected database error occurred",
                db_url=str(database_url),
                db_type="SQLAlchemy",
            )

    def _ensure_admin_user(self, admin_token: str):
        if not admin_token:
            admin_token = f"or-{secrets.token_hex(16)}"
        hashed_token = self.hash_token(admin_token)
        with self.Session() as s:
            admin = s.query(User).filter_by(id=1).first()
            if not admin:
                admin = User(
                    display_name="Admin",
                    token=hashed_token,
                    is_admin=True,
                )
                s.add(admin)
                s.commit()
                self.logger.info("Created admin user")
            else:
                admin.is_admin = True
                admin.token = hashed_token
                s.commit()
                self.logger.info("Upgraded existing user to admin")

    def list_partition_files(self, partition: str, limit: int | None = None):
        """List files in a partition with optional limit - Optimized by querying File table directly"""
        log = self.logger.bind(partition=partition)
        with self.Session() as session:
            log.debug("Listing partition files")

            # Query files directly - if partition doesn't exist, files will be empty
            files_query = session.query(File).filter(File.partition_name == partition)
            if limit is not None:
                files_query = files_query.limit(limit)

            files = files_query.all()

            # If no files found
            if not files:
                log.warning("Partition doesn't exist or has no files")
                return {}

            result = {
                "files": [file.to_dict() for file in files],
            }

            log.info(f"Listed {len(files)} files from partition")
            return result

    def add_file_to_partition(
        self,
        file_id: str,
        partition: str,
        file_metadata: dict | None = None,
        user_id: int | None = None,
    ):
        """Add a file to a partition - Optimized with direct partition lookup"""
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                existing_file = (
                    session.query(File.id).filter(File.file_id == file_id, File.partition_name == partition).first()
                )
                if existing_file:
                    log.warning("File already exists")
                    return False

                partition_obj = session.query(Partition).filter(Partition.partition == partition).first()
                if not partition_obj:
                    partition_obj = Partition(partition=partition)
                    session.add(partition_obj)
                    log.info("Created new partition")

                    membership = PartitionMembership(partition_name=partition, user_id=user_id, role="owner")
                    session.add(membership)

                # Add file to partition
                file = File(
                    file_id=file_id,
                    partition_name=partition,  # Use string directly
                    file_metadata=file_metadata,
                )

                session.add(file)
                session.commit()
                log.info("Added file successfully")
                return True
            except Exception as e:
                session.rollback()
                log.exception("Error adding file to partition", error=str(e))
                raise VDBInsertError(
                    "An unexpected database error occurred",
                    file_id=file_id,
                    partition=partition,
                )

    def remove_file_from_partition(self, file_id: str, partition: str):
        """Remove a file from its partition - Optimized without join"""
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                # Direct filter without join (uses composite index)
                file = session.query(File).filter(File.file_id == file_id, File.partition_name == partition).first()
                if file:
                    session.delete(file)
                    session.commit()
                    log.info(f"Removed file {file_id} from partition {partition}")
                    return True
                log.warning("File not found in partition")
                return False
            except Exception as e:
                session.rollback()
                log.exception("Error removing file", error=str(e))
                raise VDBDeleteError(
                    "An unexpected database error occurred",
                    file_id=file_id,
                    partition=partition,
                )

    def delete_partition(self, partition: str):
        """Delete a partition and all its files"""
        with self.Session() as session:
            partition_obj = session.query(Partition).filter_by(partition=partition).first()
            if partition_obj:
                session.delete(partition_obj)  # Will delete all files due to cascade
                session.commit()
                self.logger.info("Deleted partition", partition=partition)
                return True
            else:
                self.logger.info("Partition does not exist", partition=partition)
            return False

    def list_partitions(self):
        """List all existing partitions"""
        with self.Session() as session:
            partitions = session.query(Partition).all()
            return [partition.to_dict() for partition in partitions]

    def get_partition_file_count(self, partition: str):
        """Get the count of files in a partition - Optimized with direct count"""
        with self.Session() as session:
            # Optimized: Direct count query instead of loading partition and files
            return session.query(File).filter(File.partition_name == partition).count()

    def get_total_file_count(self):
        """Get the total count of files across all partitions"""
        with self.Session() as session:
            return session.query(File).count()

    def partition_exists(self, partition: str):
        """Check if a partition exists by its key - Optimized with exists()"""
        with self.Session() as session:
            # Optimized: Use exists() for better performance
            return session.query(session.query(Partition).filter(Partition.partition == partition).exists()).scalar()

    def file_exists_in_partition(self, file_id: str, partition: str):
        """Check if a file exists in a specific partition - Optimized without join"""
        with self.Session() as session:
            # Optimized: Direct filter without join, use exists() for better performance
            return session.query(
                session.query(File).filter(File.file_id == file_id, File.partition_name == partition).exists()
            ).scalar()

    # Domains

    def get_file_ids_by_domains(self, partition: str | None, domains: list[str]) -> list[str]:
        """Get file_ids matching ANY of the given domains in a partition (or all partitions if None)."""
        with self.Session() as session:
            query = session.query(FileDomain.file_id).filter(FileDomain.domain.in_(domains))
            if partition is not None:
                query = query.filter(FileDomain.partition_name == partition)
            rows = query.distinct().all()
            return [row[0] for row in rows]

    def set_file_domains(self, file_id: str, partition: str, domains: list[str]):
        """Replace all domains for a file with the given list."""
        with self.Session() as session:
            try:
                session.query(FileDomain).filter(
                    FileDomain.file_id == file_id,
                    FileDomain.partition_name == partition,
                ).delete()
                for domain in domains:
                    session.add(FileDomain(file_id=file_id, partition_name=partition, domain=domain))
                session.commit()
            except Exception as e:
                session.rollback()
                self.logger.exception("Error setting file domains", error=str(e))
                raise VDBInsertError(
                    "An unexpected database error occurred",
                    file_id=file_id,
                    partition=partition,
                )

    def get_file_domains(self, file_id: str, partition: str) -> list[str]:
        """Get domains for a file in a partition."""
        with self.Session() as session:
            rows = (
                session.query(FileDomain.domain)
                .filter(FileDomain.file_id == file_id, FileDomain.partition_name == partition)
                .all()
            )
            return [row[0] for row in rows]

    def list_partition_domains(self, partition: str) -> list[str]:
        """List all unique domains in a partition."""
        with self.Session() as session:
            rows = (
                session.query(FileDomain.domain)
                .filter(FileDomain.partition_name == partition)
                .distinct()
                .all()
            )
            return [row[0] for row in rows]

    # Users

    def create_user(
        self,
        display_name: str | None = None,
        external_user_id: str | None = None,
        is_admin: bool = False,
    ) -> dict:
        """Create a user and generate an API token for them."""
        with self.Session() as s:
            token = f"or-{secrets.token_hex(16)}"
            hashed_token = self.hash_token(token)

            user = User(
                display_name=display_name,
                external_user_id=external_user_id,
                token=hashed_token,
                is_admin=is_admin,
            )
            s.add(user)
            s.commit()
            s.refresh(user)

            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "token": token,
                "is_admin": user.is_admin,
            }

    def list_users(self) -> list[dict]:
        with self.Session() as s:
            users = s.query(User).all()
            return [
                {
                    "id": u.id,
                    "display_name": u.display_name,
                    "external_user_id": u.external_user_id,
                    "is_admin": u.is_admin,
                    "created_at": u.created_at.isoformat(),
                }
                for u in users
            ]

    def get_user_by_token(self, token: str) -> dict | None:
        with self.Session() as s:
            hashed_token = self.hash_token(token)
            user = s.query(User).filter(User.token == hashed_token).first()
            if not user:
                return None

            memberships = [
                {
                    "partition": m.partition_name,
                    "role": m.role,
                    "added_at": m.added_at.isoformat(),
                }
                for m in user.memberships
            ]

            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "is_admin": user.is_admin,
                "memberships": memberships,
            }

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self.Session() as s:
            user = s.query(User).filter(User.id == user_id).first()
            if not user:
                return None

            memberships = [
                {
                    "partition": m.partition_name,
                    "role": m.role,
                    "added_at": m.added_at.isoformat(),
                }
                for m in user.memberships
            ]

            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "is_admin": user.is_admin,
                "memberships": memberships,
            }

    def delete_user(self, user_id: int) -> bool:
        with self.Session() as s:
            user = s.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            s.delete(user)
            s.commit()
            return True

    def regenerate_user_token(self, user_id: int) -> dict:
        with self.Session() as s:
            user = s.query(User).filter(User.id == user_id).first()
            new_token = f"or-{secrets.token_hex(16)}"
            hashed_token = self.hash_token(new_token)
            user.token = hashed_token
            s.commit()
            s.refresh(user)

            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "token": new_token,
                "is_admin": user.is_admin,
            }

    # Memberships
    def list_partition_members(self, partition: str) -> list[dict]:
        with self.Session() as s:
            if not s.query(Partition).filter(Partition.partition == partition).first():
                self.logger.warning(f"Partition '{partition}' does not exist.")
                return []
            ms = s.query(PartitionMembership).filter_by(partition_name=partition).all()
            return [
                {
                    "user_id": m.user_id,
                    "role": m.role,
                    "added_at": m.added_at.isoformat(),
                }
                for m in ms
            ]

    def add_partition_member(self, partition: str, user_id: int, role: str) -> bool:
        with self.Session() as s:
            if not s.query(Partition).filter(Partition.partition == partition).first():
                s.add(Partition(partition=partition))
            m = s.query(PartitionMembership).filter_by(partition_name=partition, user_id=user_id).first()
            if m:
                m.role = role
            else:
                s.add(PartitionMembership(partition_name=partition, user_id=user_id, role=role))
            s.commit()
            return True

    def remove_partition_member(self, partition: str, user_id: int) -> bool:
        with self.Session() as s:
            m = s.query(PartitionMembership).filter_by(partition_name=partition, user_id=user_id).first()
            if not m:
                return False
            s.delete(m)
            s.commit()
            return True

    def update_partition_member_role(self, partition: str, user_id: int, new_role: str) -> bool:
        with self.Session() as s:
            m = s.query(PartitionMembership).filter_by(partition_name=partition, user_id=user_id).first()
            if not m:
                return False
            m.role = new_role
            s.commit()
            return True

    def create_partition(self, partition: str, user_id: int):
        with self.Session() as s:
            if s.query(Partition).filter(Partition.partition == partition).first():
                self.logger.warning(f"Partition '{partition}' already exists.")
                return
            p = Partition(partition=partition)
            s.add(p)
            # Add creator as owner
            m = PartitionMembership(partition_name=partition, user_id=user_id, role="owner")
            s.add(m)
            s.commit()
            self.logger.info(f"Partition '{partition}' created by user_id {user_id}.")

    def list_user_partitions(self, user_id: int):
        """Return full partition objects (to_dict) with role for a given user."""
        with self.Session() as s:
            # Join Partition and PartitionMembership
            results = (
                s.query(Partition, PartitionMembership.role)
                .join(
                    PartitionMembership,
                    Partition.partition == PartitionMembership.partition_name,
                )
                .filter(PartitionMembership.user_id == user_id)
                .all()
            )

            partitions = []
            for partition_obj, role in results:
                d = partition_obj.to_dict()
                d["role"] = role
                partitions.append(d)

            return partitions

    def user_exists(self, user_id: int) -> bool:
        with self.Session() as s:
            return s.query(User).filter(User.id == user_id).first() is not None

    def user_is_partition_member(self, user_id: int, partition: str) -> bool:
        with self.Session() as s:
            return s.query(PartitionMembership).filter_by(user_id=user_id, partition_name=partition).first() is not None

    def hash_token(self, token: str) -> str:
        """Return a SHA-256 hash of a token string."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
