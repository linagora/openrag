"""Regression test for #380 — app_front.py must raise on startup when
CHAINLIT_AUTH_SECRET is unset, instead of falling back to a hardcoded
default secret.
"""

import os
import sys

import pytest

_FIX_SOURCE = os.path.dirname(__file__) + "/app_front.py"


def test_no_hardcoded_default_secret_assignment_in_source():
    """The fall-through to a literal default secret must be gone.

    The bug was an assignment that substituted a hardcoded value when the
    env var was unset. We assert that assignment is not present in source.
    """
    with open(_FIX_SOURCE) as f:
        content = f.read()
    assert 'os.environ["CHAINLIT_AUTH_SECRET"] = "default_secret_for_openrag_ui"' not in content, (
        "The hardcoded chainlit-auth secret assignment must not be reintroduced"
    )


def test_app_front_raises_when_secret_missing(monkeypatch):
    """Importing app_front.py without CHAINLIT_AUTH_SECRET must raise."""
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CHAINLIT_AUTH_SECRET", raising=False)
    monkeypatch.setenv("AUTH_MODE", "token")
    # Prevent load_dotenv() inside app_front.py from re-loading CHAINLIT_AUTH_SECRET
    # from a .env file that may have it set (e.g. local dev environment).
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)

    # Drop the previously-imported app_front module so the import body re-runs.
    for mod in [m for m in sys.modules if m == "app_front" or m.endswith(".app_front")]:
        sys.modules.pop(mod, None)

    import importlib

    spec = importlib.util.spec_from_file_location("app_front_test", _FIX_SOURCE)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="CHAINLIT_AUTH_SECRET"):
        spec.loader.exec_module(module)
