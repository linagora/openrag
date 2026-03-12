import hashlib
import os
import secrets

from config import load_config
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy_utils import (
    create_database,
    database_exists,
)
from utils.exceptions.vectordb import *
from utils.logger import get_logger

from .models import Base, File, Partition, PartitionMembership, User, Workspace, WorkspaceFile

logger = get_logger()
config = load_config()

DEFAULT_FILE_QUOTA = config.rdb.get("default_file_quota", -1)


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

    def create_user(
        self,
        display_name: str | None = None,
        external_user_id: str | None = None,
        is_admin: bool = False,
        file_quota: int | None = None,
    ) -> dict:
        """Create a user and generate an API token for them."""
        with self.Session() as s:
            token = f"or-{secrets.token_hex(16)}"
            hashed_token = self.hash_token(token)

            if self.file_quota_per_user > 0 and file_quota is None:
                file_quota = self.file_quota_per_user  # default to default quota

            user = User(
                display_name=display_name,
                external_user_id=external_user_id,
                token=hashed_token,
                is_admin=is_admin,
                file_quota=file_quota,
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

    def update_user_quota(self, user_id: int, file_quota: int | None) -> dict:
        """
        Update a user's file quota.
        - None: Use global default (DEFAULT_FILE_QUOTA env var)
        - <0: Unlimited
        - >=0: Specific limit
        """
        with self.Session() as s:
            user = s.query(User).filter(User.id == user_id).first()
            user.file_quota = file_quota
            s.commit()
            self.logger.info(f"Updated file_quota for user {user_id} to {file_quota}")
            s.refresh(user)

            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "is_admin": user.is_admin,
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

                    UNION ALL

                    -- Recursive case: get parent
                    SELECT f.id, f.file_id, f.partition_name, f.parent_id,
                        f.file_metadata, f.relationship_id, a.depth + 1
                    FROM files f
                    INNER JOIN ancestors a ON f.file_id = a.parent_id
                        AND f.partition_name = a.partition_name
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
        """Delete workspace, return list of orphaned file_ids (files only in this workspace)."""
        with self.Session() as session:
            # Find files only in this workspace (not in any other)
            subq = select(WorkspaceFile.file_id).where(WorkspaceFile.workspace_id != workspace_id).subquery()
            result = session.execute(
                select(WorkspaceFile.file_id)
                .where(WorkspaceFile.workspace_id == workspace_id)
                .where(WorkspaceFile.file_id.notin_(select(subq.c.file_id)))
            )
            orphaned_file_ids = [r[0] for r in result.all()]

            # Delete workspace (cascades workspace_files)
            session.execute(delete(Workspace).where(Workspace.workspace_id == workspace_id))
            session.commit()
            return orphaned_file_ids

    def add_files_to_workspace(self, workspace_id: str, file_ids: list[str]):
        with self.Session() as session:
            for fid in file_ids:
                stmt = pg_insert(WorkspaceFile).values(workspace_id=workspace_id, file_id=fid)
                stmt = stmt.on_conflict_do_nothing(constraint="uix_workspace_file")
                session.execute(stmt)
            session.commit()

    def remove_file_from_workspace(self, workspace_id: str, file_id: str) -> bool:
        """Remove a file from a workspace. Returns True if the association existed, False otherwise."""
        with self.Session() as session:
            result = session.execute(
                delete(WorkspaceFile).where(
                    WorkspaceFile.workspace_id == workspace_id,
                    WorkspaceFile.file_id == file_id,
                )
            )
            session.commit()
            return result.rowcount > 0

    def list_workspace_files(self, workspace_id: str) -> list[str]:
        with self.Session() as session:
            result = session.execute(select(WorkspaceFile.file_id).where(WorkspaceFile.workspace_id == workspace_id))
            return [r[0] for r in result.all()]

    def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        """Return the workspace IDs that contain the given file, scoped to the given partition."""
        with self.Session() as session:
            ws_ids = select(Workspace.workspace_id).where(Workspace.partition_name == partition)
            result = session.execute(
                select(WorkspaceFile.workspace_id).where(
                    WorkspaceFile.file_id == file_id,
                    WorkspaceFile.workspace_id.in_(ws_ids),
                )
            )
            return [r[0] for r in result.all()]

    def remove_file_from_all_workspaces(self, file_id: str, partition: str):
        """Remove file from all workspaces in the given partition — called during file deletion."""
        with self.Session() as session:
            ws_ids = select(Workspace.workspace_id).where(Workspace.partition_name == partition)
            session.execute(
                delete(WorkspaceFile).where(
                    WorkspaceFile.file_id == file_id,
                    WorkspaceFile.workspace_id.in_(ws_ids),
                )
            )
            session.commit()
