import hashlib
import os
import secrets
from datetime import datetime, timedelta

from config import load_config
from models.user import UserCreate, UserUpdate
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy_utils import (
    create_database,
    database_exists,
)
from utils.exceptions.vectordb import *
from utils.logger import get_logger

from .models import (
    Base,
    File,
    OIDCSession,
    Partition,
    PartitionMembership,
    User,
    Workspace,
    WorkspaceFile,
)

logger = get_logger()
config = load_config()

DEFAULT_FILE_QUOTA = config.rdb.default_file_quota


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
            self.file_quota_per_user = DEFAULT_FILE_QUOTA

        except Exception as e:
            raise VDBConnectionError(
                f"Failed to connect to database: {e!s}",
                db_url=database_url,
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
        relationship_id: None | str = None,
        parent_id: None | str = None,
    ):
        """Add a file to a partition with optional relationship fields.

        Args:
            file_id: Unique identifier for the file
            partition: Partition name
            file_metadata: Additional metadata as JSON
            user_id: User ID for ownership (creates partition membership)
            relationship_id: Groups related documents (e.g., email thread ID, folder path)
            parent_id: Hierarchical parent reference (e.g., parent email file_id)
        """
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
                    partition_name=partition,
                    file_metadata=file_metadata,
                    relationship_id=relationship_id,
                    parent_id=parent_id,
                    created_by=user_id,
                )

                session.add(file)
                # Increment uploader's file_count
                if user_id:
                    session.query(User).filter(User.id == user_id).update(
                        {User.file_count: User.file_count + 1}, synchronize_session=False
                    )
                session.commit()
                log.info("Added file successfully")
                return True
            except Exception:
                session.rollback()
                log.exception("Error adding file to partition")
                raise

    def remove_file_from_partition(self, file_id: str, partition: str):
        """Remove a file from its partition - Optimized without join"""
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                # Direct filter without join (uses composite index)
                file = session.query(File).filter(File.file_id == file_id, File.partition_name == partition).first()
                if file:
                    uploader_id = file.created_by
                    session.delete(file)
                    if uploader_id:
                        session.query(User).filter(User.id == uploader_id).update(
                            {User.file_count: func.greatest(User.file_count - 1, 0)},
                            synchronize_session=False,
                        )
                    session.commit()
                    log.info(f"Removed file {file_id} from partition {partition}")
                    return True
                log.warning("File not found in partition")
                return False
            except Exception as e:
                session.rollback()
                log.error(f"Error removing file: {e}")
                raise e

    def update_file_metadata_in_db(self, file_id: str, partition: str, file_metadata: dict) -> bool:
        """Update the file_metadata JSON column and structured fields for an existing file.

        Returns True if the file was found and updated, False otherwise.
        Unlike remove_file_from_partition + add_file_to_partition, this preserves
        the files.id primary key so that workspace FK references remain valid.

        If file_metadata contains keys that correspond to structured File columns
        (relationship_id, parent_id), those columns are updated too so they stay
        in sync with the JSON blob.
        """
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                file = session.query(File).filter(File.file_id == file_id, File.partition_name == partition).first()
                if not file:
                    log.warning("File not found for metadata update")
                    return False
                file.file_metadata = file_metadata
                # Sync structured columns when the corresponding keys are present
                # in the metadata payload, so PG columns never diverge from the JSON.
                if "relationship_id" in file_metadata:
                    file.relationship_id = file_metadata["relationship_id"]
                if "parent_id" in file_metadata:
                    file.parent_id = file_metadata["parent_id"]
                session.commit()
                log.info("Updated file metadata in-place")
                return True
            except Exception:
                session.rollback()
                log.exception("Error updating file metadata")
                raise

    # Sentinel object to distinguish "not provided" from explicit None.
    _UNSET = object()

    def update_file_in_partition(
        self,
        file_id: str,
        partition: str,
        file_metadata: dict | None = None,
        relationship_id: str | None | object = _UNSET,
        parent_id: str | None | object = _UNSET,
    ) -> bool:
        """Update an existing file record in-place (for PUT: new content, same file_id).

        Preserves files.id so workspace FK references stay intact.
        Unlike delete+re-add, this never touches file_count or created_by.

        Pass relationship_id=None or parent_id=None explicitly to clear a stale
        link. Omit the argument entirely to leave the column unchanged.
        """
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                file = session.query(File).filter(File.file_id == file_id, File.partition_name == partition).first()
                if not file:
                    log.warning("File not found for update")
                    return False
                if file_metadata is not None:
                    file.file_metadata = file_metadata
                if relationship_id is not self._UNSET:
                    file.relationship_id = relationship_id
                if parent_id is not self._UNSET:
                    file.parent_id = parent_id
                session.commit()
                log.info("Updated file record in-place")
                return True
            except Exception:
                session.rollback()
                log.exception("Error updating file in partition")
                raise

    def delete_partition(self, partition: str):
        """Delete a partition and all its files"""
        with self.Session() as session:
            partition_obj = session.query(Partition).filter_by(partition=partition).first()
            if partition_obj:
                # Count files per uploader before cascade deletes them
                uploader_counts = (
                    session.query(File.created_by, func.count(File.id))
                    .filter(File.partition_name == partition, File.created_by.isnot(None))
                    .group_by(File.created_by)
                    .all()
                )
                session.delete(partition_obj)  # Cascades to files and memberships
                for uploader_id, count in uploader_counts:
                    session.query(User).filter(User.id == uploader_id).update(
                        {User.file_count: func.greatest(User.file_count - count, 0)},
                        synchronize_session=False,
                    )
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

    # Users

    def create_user(self, body: UserCreate) -> dict:
        """Create a user and generate an API token for them."""
        with self.Session() as s:
            token = f"or-{secrets.token_hex(16)}"
            hashed_token = self.hash_token(token)
            file_quota = body.file_quota
            if self.file_quota_per_user > 0 and file_quota is None:
                file_quota = self.file_quota_per_user  # default to default quota

            user = User(
                display_name=body.display_name,
                external_user_id=body.external_user_id,
                email=(body.email.strip().lower() if body.email else None),
                token=hashed_token,
                is_admin=body.is_admin,
                file_quota=file_quota,
            )
            s.add(user)
            s.commit()
            s.refresh(user)

            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "email": user.email,
                "token": token,
                "is_admin": user.is_admin,
                "file_quota": user.file_quota,
                "file_count": user.file_count,
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
                    "file_quota": u.file_quota,
                    "file_count": u.file_count,
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
                "file_quota": user.file_quota,
                "file_count": user.file_count,
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
                "file_quota": user.file_quota,
                "file_count": user.file_count,
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
                "file_quota": user.file_quota,
                "file_count": user.file_count,
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

    def update_user(self, user_id: int, body: UserUpdate) -> dict:
        """Update user's profile fields. Only provided (non-None) fields are updated."""
        with self.Session() as s:
            user = s.query(User).filter(User.id == user_id).first()
            for field, value in body.model_dump(exclude_unset=True).items():
                setattr(user, field, value)

            s.commit()
            s.refresh(user)
            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "is_admin": user.is_admin,
                "created_at": user.created_at.isoformat(),
                "file_quota": user.file_quota,
                "file_count": user.file_count,
            }

    def hash_token(self, token: str) -> str:
        """Return a SHA-256 hash of a token string."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # Document relationship methods

    def get_files_by_relationship(self, partition: str, relationship_id: str) -> list[dict]:
        """Get all files sharing a relationship_id within a partition.

        Args:
            partition: Partition name
            relationship_id: The relationship group identifier

        Returns:
            List of file dictionaries
        """
        with self.Session() as session:
            files = (
                session.query(File)
                .filter(
                    File.partition_name == partition,
                    File.relationship_id == relationship_id,
                )
                .all()
            )
            return [f.to_dict() for f in files]

    def get_file_ids_by_relationship(self, partition: str, relationship_id: str) -> list[str]:
        """Get file_ids for all files sharing a relationship_id.

        Args:
            partition: Partition name
            relationship_id: The relationship group identifier

        Returns:
            List of file_id strings
        """
        with self.Session() as session:
            results = (
                session.query(File.file_id)
                .filter(
                    File.partition_name == partition,
                    File.relationship_id == relationship_id,
                )
                .all()
            )
            return [r[0] for r in results]

    def get_file_ancestors(self, partition: str, file_id: str, max_ancestor_depth: int | None = None) -> list[dict]:
        """Get all ancestors of a file using recursive CTE.

        Returns ordered list from root to the specified file (direct path only).

        Args:
            partition: Partition name
            file_id: The file identifier to find ancestors for
            max_ancestor_depth: Maximum depth to traverse (None = unlimited)

        Returns:
            List of file dictionaries ordered from root to the specified file
        """

        with self.Session() as session:
            # Recursive CTE for ancestor traversal with optional max depth
            depth_condition = "WHERE a.depth < :max_ancestor_depth" if max_ancestor_depth is not None else ""
            query = text(f"""
                WITH RECURSIVE ancestors AS (
                    -- Base case: start with the target file
                    SELECT id, file_id, partition_name, parent_id, file_metadata,
                        relationship_id, 0 as depth
                    FROM files
                    WHERE file_id = :file_id AND partition_name = :partition
                        AND relationship_id IS NOT NULL

                    UNION ALL

                    -- Recursive case: get parent
                    SELECT f.id, f.file_id, f.partition_name, f.parent_id,
                        f.file_metadata, f.relationship_id, a.depth + 1
                    FROM files f
                    INNER JOIN ancestors a ON f.file_id = a.parent_id
                        AND f.partition_name = a.partition_name
                        AND f.relationship_id IS NOT NULL
                    {depth_condition}
                )
                SELECT * FROM ancestors ORDER BY depth DESC
            """)

            params = {"file_id": file_id, "partition": partition}
            if max_ancestor_depth is not None:
                params["max_ancestor_depth"] = max_ancestor_depth

            result = session.execute(query, params)

            return [
                {
                    "file_id": row.file_id,
                    "partition": row.partition_name,
                    "parent_id": row.parent_id,
                    "relationship_id": row.relationship_id,
                    "depth": row.depth,
                    **(row.file_metadata or {}),
                }
                for row in result
            ]

    def get_ancestor_file_ids(self, partition: str, file_id: str, max_ancestor_depth: int | None = None) -> list[str]:
        """Get file_ids for all ancestors of a file.

        Returns ordered list from root to the specified file (direct path only).

        Args:
            partition: Partition name
            file_id: The file identifier to find ancestors for
            max_ancestor_depth: Maximum depth to traverse (None = unlimited)
        Returns:
            List of file_id strings ordered from root to the specified file
        """
        ancestors = self.get_file_ancestors(partition, file_id, max_ancestor_depth)
        return [a["file_id"] for a in ancestors]

    # --- Workspace methods ---

    def create_workspace(self, workspace_id: str, partition: str, user_id: int | None, display_name: str | None = None):
        with self.Session() as session:
            ws = Workspace(
                workspace_id=workspace_id, partition_name=partition, created_by=user_id, display_name=display_name
            )
            session.add(ws)
            session.commit()

    def list_workspaces(self, partition: str) -> list[dict]:
        with self.Session() as session:
            result = session.execute(select(Workspace).where(Workspace.partition_name == partition))
            return [
                {
                    "workspace_id": w.workspace_id,
                    "partition_name": w.partition_name,
                    "display_name": w.display_name,
                    "created_by": w.created_by,
                    "created_at": str(w.created_at),
                }
                for w in result.scalars()
            ]

    def get_workspace(self, workspace_id: str) -> dict | None:
        with self.Session() as session:
            result = session.execute(select(Workspace).where(Workspace.workspace_id == workspace_id))
            w = result.scalar_one_or_none()
            if not w:
                return None
            return {
                "workspace_id": w.workspace_id,
                "partition_name": w.partition_name,
                "display_name": w.display_name,
                "created_by": w.created_by,
                "created_at": str(w.created_at),
            }

    def delete_workspace(self, workspace_id: str) -> list[str]:
        """Delete workspace, return list of orphaned file_ids.

        A file is orphaned if it belongs to this workspace and no other.
        Since WorkspaceFile.file_id is now an integer FK to files.id, every
        workspace file has a backing files row, so the only condition is
        "not present in any other workspace".
        """
        with self.Session() as session:
            # File PKs present in at least one other workspace
            subq_other_ws = select(WorkspaceFile.file_id).where(WorkspaceFile.workspace_id != workspace_id)
            # Orphaned = in this workspace, not in any other
            result = session.execute(
                select(File.file_id)
                .join(WorkspaceFile, WorkspaceFile.file_id == File.id)
                .where(WorkspaceFile.workspace_id == workspace_id)
                .where(WorkspaceFile.file_id.notin_(subq_other_ws))
            )
            orphaned_file_ids = [r[0] for r in result.all()]

            # Delete workspace (cascades workspace_files)
            session.execute(delete(Workspace).where(Workspace.workspace_id == workspace_id))
            session.commit()
            return orphaned_file_ids

    def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> set[str]:
        """Return the subset of *file_ids* that actually exist in *partition*."""
        with self.Session() as session:
            result = session.execute(
                select(File.file_id).where(
                    File.partition_name == partition,
                    File.file_id.in_(file_ids),
                )
            )
            return {r[0] for r in result.all()}

    def add_files_to_workspace(self, workspace_id: str, file_ids: list[str]) -> list[str]:
        """Add files to a workspace. Returns list of file_ids that could not be resolved."""
        with self.Session() as session:
            # Resolve the workspace's partition to scope the File lookup
            workspace = session.execute(
                select(Workspace).where(Workspace.workspace_id == workspace_id)
            ).scalar_one_or_none()
            if workspace is None:
                return file_ids
            partition = workspace.partition_name

            # Bulk-resolve all file_ids → File.id in a single query
            rows = session.execute(
                select(File.file_id, File.id).where(File.file_id.in_(file_ids), File.partition_name == partition)
            ).all()
            id_map = {r[0]: r[1] for r in rows}
            missing = [fid for fid in file_ids if fid not in id_map]

            for fid, file_pk in id_map.items():
                stmt = pg_insert(WorkspaceFile).values(workspace_id=workspace_id, file_id=file_pk)
                stmt = stmt.on_conflict_do_nothing(constraint="uix_workspace_file")
                session.execute(stmt)
            session.commit()
            return missing

    def remove_file_from_workspace(self, workspace_id: str, file_id: str) -> bool:
        """Remove a file from a workspace. Returns True if the association existed, False otherwise."""
        with self.Session() as session:
            workspace = session.execute(
                select(Workspace).where(Workspace.workspace_id == workspace_id)
            ).scalar_one_or_none()
            if workspace is None:
                return False
            file_pk = session.execute(
                select(File.id).where(File.file_id == file_id, File.partition_name == workspace.partition_name)
            ).scalar_one_or_none()
            if file_pk is None:
                return False
            result = session.execute(
                delete(WorkspaceFile).where(
                    WorkspaceFile.workspace_id == workspace_id,
                    WorkspaceFile.file_id == file_pk,
                )
            )
            session.commit()
            return result.rowcount > 0

    def list_workspace_files(self, workspace_id: str) -> list[str]:
        with self.Session() as session:
            result = session.execute(
                select(File.file_id)
                .join(WorkspaceFile, WorkspaceFile.file_id == File.id)
                .where(WorkspaceFile.workspace_id == workspace_id)
            )
            return [r[0] for r in result.all()]

    def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        """Return the workspace IDs that contain the given file, scoped to the given partition."""
        with self.Session() as session:
            file_pk = session.execute(
                select(File.id).where(File.file_id == file_id, File.partition_name == partition)
            ).scalar_one_or_none()
            if file_pk is None:
                return []
            ws_ids = select(Workspace.workspace_id).where(Workspace.partition_name == partition)
            result = session.execute(
                select(WorkspaceFile.workspace_id).where(
                    WorkspaceFile.file_id == file_pk,
                    WorkspaceFile.workspace_id.in_(ws_ids),
                )
            )
            return [r[0] for r in result.all()]

    def remove_file_from_all_workspaces(self, file_id: str, partition: str):
        """Remove file from all workspaces in the given partition — called during file deletion."""
        with self.Session() as session:
            file_pk = session.execute(
                select(File.id).where(File.file_id == file_id, File.partition_name == partition)
            ).scalar_one_or_none()
            if file_pk is None:
                return
            ws_ids = select(Workspace.workspace_id).where(Workspace.partition_name == partition)
            session.execute(
                delete(WorkspaceFile).where(
                    WorkspaceFile.file_id == file_pk,
                    WorkspaceFile.workspace_id.in_(ws_ids),
                )
            )
            session.commit()

    # ------------------------------------------------------------------
    # OIDC — users lookup / external_user_id backfill
    # ------------------------------------------------------------------

    def _user_to_dict(self, user: User) -> dict:
        """Serialize a User ORM object to the dict shape used elsewhere."""
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
            "email": user.email,
            "external_user_id": user.external_user_id,
            "is_admin": user.is_admin,
            "file_quota": user.file_quota,
            "file_count": user.file_count,
            "memberships": memberships,
        }

    def get_user_by_external_id(self, external_user_id: str) -> dict | None:
        """Return the user (as dict) whose ``external_user_id`` matches, or None.

        ``external_user_id`` stores the stable OIDC ``sub`` claim in OIDC mode.
        """
        with self.Session() as s:
            user = s.query(User).filter(User.external_user_id == external_user_id).first()
            if not user:
                return None
            return self._user_to_dict(user)

    def get_user_by_email(self, email: str) -> dict | None:
        """Return the user (as dict) whose ``email`` matches exactly, or None.

        NOTE: comparison is **case-sensitive**. OIDC email claims are typically
        lowercased by the IdP, but this is not guaranteed by the spec
        (RFC 7519 §4.1 treats it as arbitrary string). Callers doing lookups
        against user-provided input should normalize upstream if needed.
        """
        with self.Session() as s:
            user = s.query(User).filter(User.email == email).first()
            if not user:
                return None
            return self._user_to_dict(user)

    def set_user_external_id(self, user_id: int, external_user_id: str) -> None:
        """Backfill ``users.external_user_id`` on first successful OIDC login.

        Behaviour:
        - if currently NULL → set to ``external_user_id`` (backfill).
        - if already equal to ``external_user_id`` → no-op.
        - if set to a *different* value → raise ValueError (identity conflict;
          caller must handle by logging and returning 403 — see AC6d).
        """
        with self.Session() as s:
            user = s.query(User).filter(User.id == user_id).first()
            if user is None:
                raise ValueError(f"user_id={user_id} does not exist")
            if user.external_user_id is None:
                user.external_user_id = external_user_id
                s.commit()
                self.logger.info(
                    f"Backfilled external_user_id for user_id={user_id}"
                )
                return
            if user.external_user_id == external_user_id:
                return  # idempotent no-op
            raise ValueError(
                "external_user_id mismatch for user_id="
                f"{user_id}: stored={user.external_user_id!r}, "
                f"incoming={external_user_id!r}"
            )

    # ------------------------------------------------------------------
    # OIDC — sessions
    # ------------------------------------------------------------------

    def _oidc_session_to_dict(self, session_row: OIDCSession) -> dict:
        """Serialize an OIDCSession ORM row to a dict. Encrypted blobs are
        passed through untouched — the caller (middleware) decrypts them."""
        return {
            "id": session_row.id,
            "user_id": session_row.user_id,
            "sub": session_row.sub,
            "sid": session_row.sid,
            "id_token_encrypted": session_row.id_token_encrypted,
            "access_token_encrypted": session_row.access_token_encrypted,
            "refresh_token_encrypted": session_row.refresh_token_encrypted,
            "access_token_expires_at": session_row.access_token_expires_at,
            "session_expires_at": session_row.session_expires_at,
            "created_at": session_row.created_at,
            "last_refresh_at": session_row.last_refresh_at,
            "revoked_at": session_row.revoked_at,
        }

    def create_oidc_session(
        self,
        *,
        user_id: int,
        sub: str,
        sid: str | None,
        session_token_plain: str,
        id_token_encrypted: bytes | None,
        access_token_encrypted: bytes | None,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at: datetime,
        session_expires_at: datetime,
    ) -> dict:
        """Insert a new OIDC session row. ``session_token_plain`` is hashed
        (SHA-256) before storage — the plaintext is never persisted.

        Returns the row as a dict. Caller is responsible for setting the
        ``openrag_session`` cookie with ``session_token_plain``.
        """
        session_token_hash = self.hash_token(session_token_plain)
        with self.Session() as s:
            row = OIDCSession(
                session_token_hash=session_token_hash,
                user_id=user_id,
                sub=sub,
                sid=sid,
                id_token_encrypted=id_token_encrypted,
                access_token_encrypted=access_token_encrypted,
                refresh_token_encrypted=refresh_token_encrypted,
                access_token_expires_at=access_token_expires_at,
                session_expires_at=session_expires_at,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            self.logger.bind(user_id=user_id, sid=sid).info("Created OIDC session")
            return self._oidc_session_to_dict(row)

    def get_oidc_session_by_token(self, session_token_plain: str) -> dict | None:
        """Look up an OIDC session by its plaintext cookie token.

        Returns None if:
        - no row with matching ``session_token_hash``
        - the row is revoked (``revoked_at IS NOT NULL``)
        - the session has expired (``session_expires_at < now()``)
        """
        session_token_hash = self.hash_token(session_token_plain)
        now = datetime.now()
        with self.Session() as s:
            row = (
                s.query(OIDCSession)
                .filter(OIDCSession.session_token_hash == session_token_hash)
                .first()
            )
            if row is None:
                return None
            if row.revoked_at is not None:
                return None
            if row.session_expires_at < now:
                return None
            return self._oidc_session_to_dict(row)

    def get_oidc_session_by_id(self, session_id: int) -> dict | None:
        """Look up an OIDC session by primary key.

        Used by the refresh-token stampede guard in
        ``components.auth.refresh.refresh_session_if_needed``: when a concurrent
        request may have already rotated the tokens, the helper re-reads the row
        to see whether it can reuse the fresh tokens instead of calling the IdP
        with the (now-invalidated) old refresh_token.

        Returns the row as a dict, or ``None`` if the row does not exist, is
        revoked, or the hard session cap has elapsed.
        """
        now = datetime.now()
        with self.Session() as s:
            row = s.query(OIDCSession).filter(OIDCSession.id == session_id).first()
            if row is None:
                return None
            if row.revoked_at is not None:
                return None
            if row.session_expires_at < now:
                return None
            return self._oidc_session_to_dict(row)

    def update_oidc_session_tokens(
        self,
        *,
        session_id: int,
        access_token_encrypted: bytes,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at: datetime,
    ) -> None:
        """Persist refreshed tokens after a successful refresh_token exchange.

        Also bumps ``last_refresh_at`` to ``now()``. Does NOT extend
        ``session_expires_at`` — the hard session cap is set at creation time
        and is unaffected by access-token rotation.

        The row is locked with ``SELECT ... FOR UPDATE`` so that concurrent
        refresh calls on the same session serialize at the DB level (Postgres).
        SQLite silently ignores the lock hint, which is fine for tests — the
        ``last_refresh_at`` short-circuit in :mod:`components.auth.refresh`
        already handles the common stampede case without needing a real lock.
        """
        with self.Session() as s:
            row = (
                s.query(OIDCSession)
                .filter(OIDCSession.id == session_id)
                .with_for_update()
                .first()
            )
            if row is None:
                raise ValueError(f"oidc_session id={session_id} does not exist")
            row.access_token_encrypted = access_token_encrypted
            if refresh_token_encrypted is not None:
                row.refresh_token_encrypted = refresh_token_encrypted
            row.access_token_expires_at = access_token_expires_at
            row.last_refresh_at = datetime.now()
            s.commit()

    def revoke_oidc_sessions_by_sid(self, sid: str) -> int:
        """Revoke all non-revoked sessions matching the given OIDC ``sid``.

        Used by the OIDC Back-Channel Logout flow — the IdP POSTs a signed
        logout_token with a ``sid`` claim; we mark every session with that
        sid as revoked. Returns the count affected.
        """
        now = datetime.now()
        with self.Session() as s:
            stmt = (
                update(OIDCSession)
                .where(OIDCSession.sid == sid)
                .where(OIDCSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            result = s.execute(stmt)
            s.commit()
            count = result.rowcount or 0
            self.logger.bind(sid=sid, count=count).info(
                "Revoked OIDC sessions by sid"
            )
            return count

    def revoke_oidc_session_by_id(self, session_id: int) -> None:
        """Revoke a single session by primary key. Used by RP-initiated logout."""
        now = datetime.now()
        with self.Session() as s:
            row = s.query(OIDCSession).filter(OIDCSession.id == session_id).first()
            if row is None:
                return
            if row.revoked_at is None:
                row.revoked_at = now
                s.commit()
                self.logger.bind(session_id=session_id).info("Revoked OIDC session")

    def cleanup_expired_oidc_sessions(self) -> int:
        """Delete rows whose ``session_expires_at`` is older than 7 days.

        A retention window after expiry helps post-mortem debugging while
        keeping the table bounded. Intended to be called by a future cron.
        Returns the number of rows deleted.
        """
        cutoff = datetime.now() - timedelta(days=7)
        with self.Session() as s:
            stmt = delete(OIDCSession).where(OIDCSession.session_expires_at < cutoff)
            result = s.execute(stmt)
            s.commit()
            count = result.rowcount or 0
            if count:
                self.logger.info(f"Cleaned up {count} expired OIDC sessions")
            return count
