"""Shared Chainlit auth handoff constants."""

import os

CHAINLIT_TOKEN_COOKIE_NAME = "openrag_chainlit_token"
CHAINLIT_TOKEN_COOKIE_PATH = "/chainlit"
CHAINLIT_TOKEN_COOKIE_MAX_AGE_SECONDS = 120
CHAINLIT_AUTH_COOKIE_NAME = os.environ.get("CHAINLIT_AUTH_COOKIE_NAME", "access_token")
