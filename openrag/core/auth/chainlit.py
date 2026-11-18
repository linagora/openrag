"""Shared Chainlit auth handoff constants."""

import os

CHAINLIT_TOKEN_COOKIE_NAME = "openrag_chainlit_token"
CHAINLIT_TOKEN_COOKIE_PATH = "/chainlit"
CHAINLIT_TOKEN_COOKIE_MAX_AGE_SECONDS = 120
CHAINLIT_AUTH_COOKIE_NAME = os.environ.get("CHAINLIT_AUTH_COOKIE_NAME", "access_token")
CHAINLIT_LOGOUT_COOKIE_NAME = "openrag_chainlit_logout"
CHAINLIT_LOGOUT_COOKIE_MAX_AGE_SECONDS = 86400
CHAINLIT_LOGOUT_SIGNAL_HEADER = "x-openrag-chainlit-logout-signal"
# Set by Chat logout in OIDC mode: the page reload that follows is sent to
# /auth/logout, which ends the IdP session. That logout reads the IdP hint from
# the session Chat logout just revoked, for this long at most.
CHAINLIT_IDP_LOGOUT_COOKIE_NAME = "openrag_chainlit_idp_logout"
CHAINLIT_IDP_LOGOUT_MAX_AGE_SECONDS = 60
