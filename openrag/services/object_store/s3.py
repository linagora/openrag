"""S3-compatible :class:`ObjectStore` — the production backend (MinIO in-stack).

Wraps the synchronous ``boto3`` S3 client; every call runs in a worker thread
(``asyncio.to_thread``) so a slow network round-trip never blocks the event loop.
Targets the MinIO already shipped in the stack (Milvus's dependency) but speaks
plain S3, so AWS S3 / GCS-S3 / Ceph work with only config changes.

Lifecycle: producers write transient scratch objects and best-effort delete them
after the parse completes; a bucket TTL/expiry policy is the backstop that reaps
orphans left by a crashed producer. This adapter owns *access*, not the retention
policy (that is set on the bucket by ops).

Thread-safety: boto3 low-level clients are thread-safe for method calls, so a
single client instance is shared across the ``to_thread`` pool. The bucket is
ensured exactly once (double-checked lock) before the first write.
"""

from __future__ import annotations

import asyncio

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from core.ports.object_store import ObjectNotFound, ObjectStore
from core.utils.logging import get_logger

logger = get_logger()

# S3 API quirk: us-east-1 is the default region and rejects an explicit
# LocationConstraint; every other region requires it. MinIO tolerates both.
_DEFAULT_REGION = "us-east-1"


def _is_not_found(exc: ClientError) -> bool:
    err = exc.response.get("Error", {})
    return (
        err.get("Code") in ("NoSuchKey", "404", "NoSuchBucket")
        or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
    )


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = _DEFAULT_REGION,
    ) -> None:
        self._bucket = bucket
        self._region = region
        # Retries with adaptive backoff for transient 5xx / throttling; path-style
        # addressing so it works against MinIO without DNS-style bucket hosts.
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
        self._bucket_ready = False
        self._bucket_lock = asyncio.Lock()

    async def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        async with self._bucket_lock:
            if self._bucket_ready:
                return
            await asyncio.to_thread(self._ensure_bucket_sync)
            self._bucket_ready = True

    def _ensure_bucket_sync(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        try:
            if self._region == _DEFAULT_REGION:
                self._client.create_bucket(Bucket=self._bucket)
            else:
                self._client.create_bucket(
                    Bucket=self._bucket,
                    CreateBucketConfiguration={"LocationConstraint": self._region},
                )
            logger.info(f"created object-store bucket {self._bucket!r}")
        except ClientError as exc:
            # Lost a create race with another replica — the bucket now exists.
            if exc.response.get("Error", {}).get("Code") in (
                "BucketAlreadyOwnedByYou",
                "BucketAlreadyExists",
            ):
                return
            raise

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        await self._ensure_bucket()
        extra = {"ContentType": content_type} if content_type else {}
        await asyncio.to_thread(self._client.put_object, Bucket=self._bucket, Key=key, Body=data, **extra)

    async def get(self, key: str) -> bytes:
        try:
            resp = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectNotFound(key) from exc
            raise
        return await asyncio.to_thread(resp["Body"].read)

    async def delete(self, key: str) -> None:
        # S3 delete_object is idempotent: deleting an absent key returns 204.
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)


__all__ = ["S3ObjectStore"]
