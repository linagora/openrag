# Adapter shim -- canonical code moved to services.auth.refresh (Phase 6F).
from services.auth.refresh import refresh_session_if_needed

__all__ = ["refresh_session_if_needed"]
