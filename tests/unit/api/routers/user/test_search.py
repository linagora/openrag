from types import SimpleNamespace

from api.dependencies.auth import (
    current_user,
    current_user_or_admin_partitions_list,
    current_user_partitions,
)
from api.error_handlers import register_error_handlers
from api.routers.user.search import router as search_router
from di.providers import get_auth_service, get_partition_service, get_retrieval_service, get_workspace_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _AuthService:
    @staticmethod
    def check_partition_access(**_kwargs):
        return True


class _PartitionService:
    async def partition_exists(self, _partition):
        return False


class _RetrievalService:
    async def search(self, **_kwargs):
        raise AssertionError("search must not run without an authorized partition scope")


def _client(*, user_partitions):
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[current_user] = lambda: {"id": 7, "is_admin": False}
    app.dependency_overrides[current_user_partitions] = lambda: user_partitions
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: [
        row["partition"] for row in user_partitions
    ]
    app.dependency_overrides[get_auth_service] = lambda: _AuthService
    app.dependency_overrides[get_partition_service] = lambda: _PartitionService()
    app.dependency_overrides[get_retrieval_service] = lambda: _RetrievalService()
    app.dependency_overrides[get_workspace_service] = lambda: object()
    return TestClient(app)


def test_search_default_all_fails_closed_when_user_has_no_partitions():
    response = _client(user_partitions=[]).get("/search", params={"text": "hello"})

    assert response.status_code == 403
    assert response.json()["detail"] == "No accessible partitions"


def test_search_rejects_cross_tenant_filter_injection():
    # An authorized viewer on one partition must not be able to break out of the
    # partition scope via an unbalanced-paren filter. The guard fires during
    # dependency resolution, so retrieval never runs (``_RetrievalService.search``
    # raises if it does) and the request is rejected with 400 — not a 500 or a
    # leaked result set.
    response = _client(user_partitions=[{"partition": "mine", "role": "viewer"}]).get(
        "/search", params={"text": "hello", "filter": "1==1) or (1==1"}
    )

    assert response.status_code == 400
    # Confirm the rejection is the filter guard specifically — the code is
    # embedded in the detail (``"[INVALID_FILTER]: ..."``), not a generic 400.
    assert "INVALID_FILTER" in response.json()["detail"]


def test_search_file_binds_file_id_via_filter_params():
    # The by-file route must bind file_id through filter_params (parameterized),
    # not by string-templating it into `filter` — the store ANDs each
    # filter_params key with the raw filter expr and parenthesises every
    # operand, so a caller filter like ``page > 5 OR 1==1`` cannot widen the
    # file_id scope.
    from api.dependencies.auth import require_partition_viewer
    from api.dependencies.files import validate_file_id

    captured: dict = {}

    class _CapturingRetrieval:
        async def search(self, **kwargs):
            captured.update(kwargs)
            return []

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: None
    app.dependency_overrides[validate_file_id] = lambda: "abc123"
    app.dependency_overrides[get_retrieval_service] = lambda: _CapturingRetrieval()

    resp = TestClient(app).get(
        "/search/partition/mine/file/abc123", params={"text": "q", "filter": "page > 5 OR page < 2"}
    )

    assert resp.status_code == 200
    assert captured["filter"] == "page > 5 OR page < 2"
    assert captured["filter_params"] == {"file_id": "abc123"}


# --------------------------------------------------------------------------- #
# Workspace scoping (issue #706)
# --------------------------------------------------------------------------- #


class _CapturingRetrieval:
    def __init__(self):
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return []


class _FakeWorkspaces:
    def __init__(self, scope=None):
        self._scope = scope
        self.resolve_scope_calls: list[tuple] = []

    async def resolve_scope(self, workspace_id, allowed_partitions):
        self.resolve_scope_calls.append((workspace_id, list(allowed_partitions)))
        return self._scope


def _client_with_workspace(*, user_partitions, workspaces, retrieval):
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[current_user] = lambda: {"id": 7, "is_admin": False}
    app.dependency_overrides[current_user_partitions] = lambda: user_partitions
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: [
        row["partition"] for row in user_partitions
    ]
    app.dependency_overrides[get_auth_service] = lambda: _AuthService
    app.dependency_overrides[get_partition_service] = lambda: _PartitionService()
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval
    app.dependency_overrides[get_workspace_service] = lambda: workspaces
    return TestClient(app)


def test_search_multiple_partitions_valid_workspace_scopes_partition_and_files():
    scope = SimpleNamespace(workspace_id="w1", partition="mine", file_ids=["fa", "fb"])
    retrieval = _CapturingRetrieval()
    client = _client_with_workspace(
        user_partitions=[{"partition": "mine", "role": "viewer"}, {"partition": "other", "role": "viewer"}],
        workspaces=_FakeWorkspaces(scope),
        retrieval=retrieval,
    )
    resp = client.get("/search", params={"text": "hello", "workspace": "w1"})

    assert resp.status_code == 200
    call = retrieval.calls[0]
    # Must narrow to the workspace's own partition, not the full accessible set.
    assert call["partitions"] == ["mine"]
    assert call["filter_params"] == {"file_id": ["fa", "fb"]}


def test_search_multiple_partitions_invalid_workspace_404s_and_skips_search():
    retrieval = _CapturingRetrieval()
    client = _client_with_workspace(
        user_partitions=[{"partition": "mine", "role": "viewer"}],
        workspaces=_FakeWorkspaces(None),
        retrieval=retrieval,
    )
    resp = client.get("/search", params={"text": "hello", "workspace": "ghost"})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Workspace not found"
    assert retrieval.calls == []  # never falls back to an unscoped search


def test_search_one_partition_valid_workspace_scopes_files():
    from api.dependencies.auth import require_partition_viewer

    scope = SimpleNamespace(workspace_id="w1", partition="mine", file_ids=["fa"])
    retrieval = _CapturingRetrieval()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: None
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval
    app.dependency_overrides[get_workspace_service] = lambda: _FakeWorkspaces(scope)

    resp = TestClient(app).get("/search/partition/mine", params={"text": "hello", "workspace": "w1"})

    assert resp.status_code == 200
    assert retrieval.calls[0]["filter_params"] == {"file_id": ["fa"]}


def test_search_one_partition_empty_workspace_forwards_empty_file_ids():
    from api.dependencies.auth import require_partition_viewer

    scope = SimpleNamespace(workspace_id="w1", partition="mine", file_ids=[])
    retrieval = _CapturingRetrieval()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: None
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval
    app.dependency_overrides[get_workspace_service] = lambda: _FakeWorkspaces(scope)

    resp = TestClient(app).get("/search/partition/mine", params={"text": "hello", "workspace": "w1"})

    assert resp.status_code == 200
    # Must stay an explicit empty allowlist (fails closed), never omitted/None.
    assert retrieval.calls[0]["filter_params"] == {"file_id": []}


def test_search_one_partition_invalid_workspace_404s():
    from api.dependencies.auth import require_partition_viewer

    retrieval = _CapturingRetrieval()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: None
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval
    app.dependency_overrides[get_workspace_service] = lambda: _FakeWorkspaces(None)

    resp = TestClient(app).get("/search/partition/mine", params={"text": "hello", "workspace": "ghost"})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Workspace not found"
    assert retrieval.calls == []
