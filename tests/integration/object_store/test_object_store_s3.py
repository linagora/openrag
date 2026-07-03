"""S3/MinIO backend against the shared ObjectStore conformance contract.

Same assertions the in-memory backend passes — proving the S3 adapter honors the
exact contract the marker-serve client (producer) and worker (consumer) depend
on. Runs only when an S3/MinIO endpoint is reachable (see conftest); otherwise
skipped.
"""

from __future__ import annotations

import pytest
from support.object_store_contract import ObjectStoreContract

pytestmark = pytest.mark.integration


class TestS3ObjectStore(ObjectStoreContract):
    """`object_store` fixture provided by the local conftest (MinIO-backed)."""
