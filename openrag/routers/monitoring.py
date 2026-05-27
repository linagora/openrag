"""Legacy import path for the monitoring router (Phase 10F shim).

The ``/metrics`` endpoint now lives at
:mod:`openrag.api.routers.admin.monitoring`. Phase 10C had already
moved the middleware to :mod:`openrag.api.middleware.instrumentation`
while leaving the endpoint here; 10F finishes the move and turns this
file into a re-export shim.

``MonitoringMiddleware`` is re-exported as an alias for the
:class:`InstrumentationMiddleware` rename so the legacy
``openrag/main.py`` keeps booting through the strangler-fig window;
Phase 12 cleanup deletes both this shim and the alias.
"""

from api.middleware.instrumentation import InstrumentationMiddleware
from api.routers.admin.monitoring import *  # noqa: F401,F403
from api.routers.admin.monitoring import router

# Legacy name kept alive for the still-active ``openrag/main.py``
# entrypoint; the new ``openrag/api/main.py`` imports the renamed
# class directly from ``api.middleware``.
MonitoringMiddleware = InstrumentationMiddleware

__all__ = ["router", "MonitoringMiddleware"]
