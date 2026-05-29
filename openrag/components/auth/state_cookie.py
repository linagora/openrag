"""Re-export shim — implementation lives in core.auth.state_cookie."""

from core.auth.state_cookie import StateCookiePayload, StateCookieSerializer

__all__ = ["StateCookieSerializer", "StateCookiePayload"]
