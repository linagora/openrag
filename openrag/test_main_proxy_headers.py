"""Regression test for #384 — uvicorn.run must be called with
``forwarded_allow_ips`` so that ``X-Forwarded-Proto`` from a non-loopback
reverse proxy is honored, instead of being silently dropped (which made
OIDC cookies ship with ``Secure=False`` even on HTTPS).
"""

import ast
import os

_MAIN_PATH = os.path.dirname(__file__) + "/main.py"


def test_uvicorn_run_passes_forwarded_allow_ips():
    """Scan main.py's AST for the uvicorn.run call that serves the app and
    assert ``forwarded_allow_ips`` is passed as a kwarg."""
    with open(_MAIN_PATH) as f:
        tree = ast.parse(f.read())

    found_with_kwarg = False
    found_any_run = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match uvicorn.run(...) — direct attribute access only
        if not (isinstance(func, ast.Attribute) and func.attr == "run"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "uvicorn"):
            continue
        found_any_run = True
        # Look for "main:app" as first positional to identify the API serve
        # call (the other uvicorn.run is for the Chainlit standalone app).
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "main:app"):
            continue
        kw_names = {kw.arg for kw in node.keywords}
        if "forwarded_allow_ips" in kw_names:
            found_with_kwarg = True

    assert found_any_run, "No uvicorn.run(...) call found in main.py"
    assert found_with_kwarg, (
        'uvicorn.run("main:app", ...) must pass forwarded_allow_ips so that '
        "X-Forwarded-Proto from a reverse proxy is honored."
    )


def test_default_forwarded_allow_ips_env_var_used():
    """The implementation should read the trusted-proxy CIDR list from
    ``UVICORN_FORWARDED_ALLOW_IPS`` to allow operators to override.
    """
    with open(_MAIN_PATH) as f:
        src = f.read()
    assert "UVICORN_FORWARDED_ALLOW_IPS" in src
