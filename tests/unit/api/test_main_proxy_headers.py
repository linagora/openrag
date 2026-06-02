"""Regression test for #384 — uvicorn.run must be called with
``forwarded_allow_ips`` so that ``X-Forwarded-Proto`` from a non-loopback
reverse proxy is honored, instead of being silently dropped (which made
OIDC cookies ship with ``Secure=False`` even on HTTPS).

Phase 10G moved the entrypoint from ``openrag/main.py`` to
``openrag/api/main.py`` and flipped uvicorn from ``main:app`` to
``api.main:app``; the test follows it so the regression guard keeps
catching kwarg drift on the new module.
"""

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

_MAIN_PATH = Path(__file__).resolve().parents[3] / "openrag" / "api" / "main.py"


def test_uvicorn_run_passes_forwarded_allow_ips():
    """Scan api/main.py's AST for the uvicorn.run call that serves the
    app and assert ``forwarded_allow_ips`` is passed as a kwarg."""
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
        # Look for "api.main:app" as first positional to identify the
        # API serve call (the other uvicorn.run is for the Chainlit
        # standalone app).
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "api.main:app"):
            continue
        kw_names = {kw.arg for kw in node.keywords}
        if "forwarded_allow_ips" in kw_names:
            found_with_kwarg = True

    assert found_any_run, "No uvicorn.run(...) call found in api/main.py"
    assert found_with_kwarg, (
        'uvicorn.run("api.main:app", ...) must pass forwarded_allow_ips so '
        "that X-Forwarded-Proto from a reverse proxy is honored."
    )


def test_default_forwarded_allow_ips_env_var_used():
    """The implementation should read the trusted-proxy CIDR list from
    ``UVICORN_FORWARDED_ALLOW_IPS`` to allow operators to override.
    """
    with open(_MAIN_PATH) as f:
        src = f.read()
    assert "UVICORN_FORWARDED_ALLOW_IPS" in src


def test_phase14_admin_routers_are_mounted():
    """Phase 14 admin routes must be exposed under stable API prefixes."""
    with open(_MAIN_PATH) as f:
        tree = ast.parse(f.read())

    mounted: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "include_router"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        router_name = node.args[0].id
        prefix = None
        tag = None
        for kw in node.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = kw.value.value
            if kw.arg == "tags" and isinstance(kw.value, ast.List) and kw.value.elts:
                first_tag = kw.value.elts[0]
                if isinstance(first_tag, ast.Attribute):
                    tag = first_tag.attr
        if prefix is not None and tag is not None:
            mounted[router_name] = f"{prefix}:{tag}"

    assert mounted["model_endpoints_router"] == "/model-endpoints:MODEL_ENDPOINTS"
    assert mounted["presets_router"] == "/presets:PRESETS"


def test_api_package_exports_app_for_legacy_uvicorn_path(monkeypatch):
    """Older images or overrides may still run ``uvicorn api:app``."""
    fake_app = object()
    fake_main = ModuleType("api.main")
    fake_main.app = fake_app
    monkeypatch.setitem(sys.modules, "api.main", fake_main)

    api_module = importlib.import_module("api")

    assert api_module.app is fake_app
