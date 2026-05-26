"""DocSerializer Ray actor — legacy re-export shim.

The implementation now lives in
``services/workers/parsers/doc_serializer.py``; this module re-exports
``DocSerializer`` so existing import paths (``services/workers/bootstrap.py``,
``services/storage/serializer_ray_shim.py``) are unaffected.
"""

from services.workers.parsers.doc_serializer import DocSerializer  # noqa: F401

__all__ = ["DocSerializer"]
