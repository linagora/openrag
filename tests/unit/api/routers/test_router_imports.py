import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "api.routers.user.chat",
        "api.routers.user.search",
        "api.routers.user.health",
        "api.routers.user.extract",
        "api.routers.admin.indexing",
        "api.routers.admin.model_endpoints",
        "api.routers.admin.partitions",
        "api.routers.admin.presets",
        "api.routers.admin.users",
        "api.routers.admin.workspaces",
        "api.routers.admin.jobs",
        "api.routers.admin.tools",
        "api.routers.admin.cluster",
        "api.routers.admin.monitoring",
        "api.routers.auth.login",
        "api.routers.auth.oidc",
    ],
)
def test_phase_10_router_module_imports(module_name):
    """Router modules should import and expose a FastAPI router."""
    module = importlib.import_module(module_name)

    assert module.router is not None
