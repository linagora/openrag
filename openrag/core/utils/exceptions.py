# TODO: Phase 2 — move exception definitions here and make the old
# location (utils/exceptions/) re-export from this module instead.
#
# For now this is a forward-compatible alias so that code within core/
# can import exceptions without reaching into the legacy utils/ path.

from utils.exceptions.base import EmbeddingError, OpenRAGError, VDBError

__all__ = ["OpenRAGError", "EmbeddingError", "VDBError"]
