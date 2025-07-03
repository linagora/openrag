from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    declarative_base,
    relationship,
    sessionmaker,
)
from utils.logger import get_logger

logger = get_logger()

Base = declarative_base()


class FileModel(BaseModel):
    file_id: str
    partition: str
    file_metadata: Dict = {}


class BasePartitionModel(BaseModel):
    partition: str
    created_at: datetime


# In the PartitionModel class
class PartitionModel(BasePartitionModel):
    files: List[FileModel] = []


class File(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    file_id = Column(String, nullable=False)
    # Foreign key points directly to the partition string
    partition_name = Column(String, ForeignKey("partitions.partition"), nullable=False)
    file_metadata = Column(JSON, nullable=True, default={})

    # relationship to the Partition object
    partition = relationship("Partition", back_populates="files")

    # Enforce uniqueness of (file_id, partition_name)
    __table_args__ = (
        UniqueConstraint("file_id", "partition_name", name="uix_file_id_partition"),
    )

    def to_dict(self):
        return self.file_metadata or {}

    def __repr__(self):
        return f"<File(id={self.id}, file_id='{self.file_id}', partition='{self.partition}')>"


# In the Partition model
class Partition(Base):
    __tablename__ = "partitions"

    id = Column(Integer, primary_key=True)
    partition = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    files = relationship(
        "File", back_populates="partition", cascade="all, delete-orphan"
    )

    def to_dict(self):
        d = {
            "partition": self.partition,
            "created_at": self.created_at.isoformat(),
            "files": self.files,
            "file_count": len(self.files),  # Count of files in this partition
        }
        return d

    def __repr__(self):
        return f"<Partition(key='{self.partition}', created_at='{self.created_at}', file_count={len(self.files)})>"


class PartitionFileManager:
    def __init__(self, database_url: str, logger=logger):
        self.engine = create_engine(database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.logger = logger

    def get_partition(self, partition: str):
        """Retrieve a partition by its key"""
        log = self.logger.bind(partition=partition)
        with self.Session() as session:
            log.debug("Fetching partition")
            partition_obj = (
                session.query(Partition).filter_by(partition=partition).first()
            )
            if partition_obj:
                log.info(f"Partition `{partition}` found")
                return partition_obj.to_dict()
            else:
                log.warning("No partition found")
                return {}

    # def create_partition(self, partition: str):
    #     """Create a new partition if it doesn't exist"""
    #     with self.Session() as session:
    #         try:
    #             partition = (
    #                 session.query(Partition).filter_by(partition=partition).first()
    #             )
    #             if not partition:
    #                 partition = Partition(partition=partition)
    #                 session.add(partition)
    #                 session.commit()
    #                 self.logger.info(f"Created new partition with key: {partition}")
    #             return partition
    #         except Exception as e:
    #             session.rollback()
    #             raise e

    def add_file_to_partition(
        self, file_id: str, partition: str, file_metadata: Optional[Dict] = None
    ):
        """Add a file to a partition"""
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                # Check if file already exists in this partition
                existing_file = (
                    session.query(File)
                    .join(Partition)
                    .filter(File.file_id == file_id, Partition.partition == partition)
                    .first()
                )
                if existing_file:
                    log.warning("File already exists")
                    return False

                # Ensure partition exists
                partition_obj = (
                    session.query(Partition).filter_by(partition=partition).first()
                )
                if not partition_obj:
                    partition_obj = Partition(partition=partition)
                    session.add(partition_obj)
                    log.info("Created new partition")

                # Add file to partition
                file = File(
                    file_id=file_id,
                    partition_name=partition_obj.partition,
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
        """Remove a file from its partition"""
        log = self.logger.bind(file_id=file_id, partition=partition)
        with self.Session() as session:
            try:
                # Find the file using a join with proper filtering
                file = (
                    session.query(File)
                    .join(Partition)
                    .filter(File.file_id == file_id, Partition.partition == partition)
                    .first()
                )
                if file:
                    session.delete(file)
                    session.commit()
                    log.info(f"Removed file {file_id} from partition {partition}")

                    # Check if partition is now empty
                    partition_obj = (
                        session.query(Partition).filter_by(partition=partition).first()
                    )
                    if partition_obj and len(partition_obj.files) == 0:
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
            partition = session.query(Partition).filter_by(partition=partition).first()
            if partition:
                session.delete(partition)  # Will delete all files due to cascade
                session.commit()
                self.logger.info("Deleted partition", partition=partition)
                return True
            else:
                self.logger.info("Partition does not exist", partition=partition)
            return False

    def list_partitions(self, **kwargs):
        """List all existing partitions"""
        with self.Session() as session:
            partitions = session.query(Partition).all()
            return [partition.to_dict(**kwargs) for partition in partitions]

    def get_partition_file_count(self, partition: str):
        """Get the count of files in a partition"""
        with self.Session() as session:
            partition_obj = (
                session.query(Partition).filter_by(partition=partition).first()
            )
            if not partition_obj:
                return 0
            return len(partition_obj.files)  # Or use a count query if you prefer

    def get_total_file_count(self):
        """Get the total count of files across all partitions"""
        with self.Session() as session:
            return session.query(File).count()

    def partition_exists(self, partition: str):
        """Check if a partition exists by its key"""
        with self.Session() as session:
            return session.query(Partition).filter_by(partition=partition).count() > 0

    def file_exists_in_partition(self, file_id: str, partition: str):
        """Check if a file exists in a specific partition"""
        with self.Session() as session:
            # Use a join to correctly filter by both file_id and partition string
            return (
                session.query(File)
                .join(Partition)  # Join the File and Partition tables
                .filter(
                    File.file_id == file_id,
                    Partition.partition == partition,  # Filter on the partition string
                )
                .count()
                > 0
            )
