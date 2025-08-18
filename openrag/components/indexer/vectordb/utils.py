from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Index,
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
from utils.logger import get_logger

logger = get_logger()

Base = declarative_base()


class File(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    file_id = Column(
        String, nullable=False, index=True
    )  # Added index for file_id lookups
    # Foreign key points directly to the partition string
    partition_name = Column(
        String, ForeignKey("partitions.partition"), nullable=False, index=True
    )  # Added index
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
    partition = Column(
        String, unique=True, nullable=False, index=True
    )  # Index already exists due to unique constraint
    created_at = Column(
        DateTime, default=datetime.now, nullable=False, index=True
    )  # Added index for time-based queries
    files = relationship(
        "File", back_populates="partition", cascade="all, delete-orphan"
    )

    def to_dict(self):
        d = {
            "partition": self.partition,
            "created_at": self.created_at.isoformat(),
        }
        return d

    def __repr__(self):
        return f"<Partition(key='{self.partition}', created_at='{self.created_at}')>"


class PartitionFileManager:
    def __init__(self, database_url: str, logger=logger):
        self.engine = create_engine(database_url)
        if not database_exists(database_url):
            create_database(database_url)

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.logger = logger

    def list_partition_files(self, partition: str, limit: Optional[int] = None):
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

            # Get total file count and partition info
            partition_obj = (
                session.query(Partition).filter_by(partition=partition).first()
            )

            result = {
                "partition": partition_obj.partition,
                "created_at": partition_obj.created_at.isoformat(),
                "files": [file.to_dict() for file in files],
            }

            log.info(f"Listed {len(files)} files from partition")
            return result

    def add_file_to_partition(
        self, file_id: str, partition: str, file_metadata: Optional[Dict] = None
    ):
        """Add a file to a partition - Optimized with direct partition lookup"""
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                existing_file = (
                    session.query(File.id)  # Only select id, not entire object
                    .filter(File.file_id == file_id, File.partition_name == partition)
                    .first()
                )
                if existing_file:
                    log.warning("File already exists")
                    return False

                partition_obj = (
                    session.query(Partition)
                    .filter(Partition.partition == partition)
                    .first()
                )
                if not partition_obj:
                    partition_obj = Partition(partition=partition)
                    session.add(partition_obj)
                    log.info("Created new partition")

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
                file = (
                    session.query(File)
                    .filter(File.file_id == file_id, File.partition_name == partition)
                    .first()
                )
                if file:
                    session.delete(file)
                    session.commit()
                    log.info(f"Removed file {file_id} from partition {partition}")

                    # Use count query instead of loading all files
                    file_count = (
                        session.query(File)
                        .filter(File.partition_name == partition)
                        .count()
                    )
                    if file_count == 0:
                        partition_obj = (
                            session.query(Partition)
                            .filter(Partition.partition == partition)
                            .first()
                        )
                        if partition_obj:
                            session.delete(partition_obj)
                            session.commit()
                            log.info("Deleted empty partition")

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
            partition_obj = (
                session.query(Partition).filter_by(partition=partition).first()
            )
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
            return session.query(
                session.query(Partition)
                .filter(Partition.partition == partition)
                .exists()
            ).scalar()

    def file_exists_in_partition(self, file_id: str, partition: str):
        """Check if a file exists in a specific partition - Optimized without join"""
        with self.Session() as session:
            # Optimized: Direct filter without join, use exists() for better performance
            return session.query(
                session.query(File)
                .filter(File.file_id == file_id, File.partition_name == partition)
                .exists()
            ).scalar()
