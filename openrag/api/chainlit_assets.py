"""Serve Chainlit's bundled root-absolute static assets (the pdf.js worker).

Chainlit is submounted at ``/chainlit``, so its HTML asset references are
rewritten to ``/chainlit/assets/*``. Its bundled pdf.js worker URL, however, is
computed at runtime in JS as the root-absolute ``/assets/pdf.worker*.mjs`` (no
mount prefix), so react-pdf fetches the worker from the origin root. That path
is not under the ``/chainlit`` auth bypass, so it returns a 403 JSON body and
the browser blocks the module worker on a bad MIME type — source PDF previews
then fail with "Failed to load PDF file". Serving the same asset files at
``/assets`` too (``AuthMiddleware`` bypasses ``/assets/*``) lets the worker load
with a JavaScript MIME type.

Both browser origins that load Chainlit must expose this route: the mounted
deployment (``api.main``) and the standalone Ray Serve Chainlit process
(``chainlit_api``, served on its own port). This mirrors how ``chainlit_api``
already replicates the ``/static`` download route for the same
separate-origin reason.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI


def mount_chainlit_root_assets(app: FastAPI) -> None:
    """Mount Chainlit's ``frontend/dist/assets`` at ``/assets`` (no-op if absent)."""
    import chainlit
    from starlette.staticfiles import StaticFiles

    # A regularly installed ``chainlit`` always has ``__file__``; a namespace
    # package or a test double (bare ``ModuleType``) may not — treat that as
    # "bundled assets unavailable" and no-op rather than raising AttributeError.
    chainlit_file = getattr(chainlit, "__file__", None)
    if not chainlit_file:
        return

    assets_dir = Path(chainlit_file).parent / "frontend" / "dist" / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="chainlit_root_assets")
