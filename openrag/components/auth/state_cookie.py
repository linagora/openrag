# Adapter shim -- canonical code moved to services.auth.state_cookie (Phase 6F).
from services.auth.state_cookie import StateCookiePayload, StateCookieSerializer

__all__ = ["StateCookiePayload", "StateCookieSerializer"]
