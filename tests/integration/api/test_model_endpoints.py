"""Model endpoint admin API integration tests."""

import uuid

MOCK_VLLM_ENDPOINT = "http://vllm:8000/v1"
MOCK_CHAT_MODEL = "mock-chat-model"


def _assert_success(response, *, context: str) -> None:
    assert 200 <= response.status_code < 300, f"{context}: {response.status_code} {response.text}"


def _delete_ignore_errors(api_client, path: str) -> None:
    try:
        api_client.delete(path)
    except Exception:
        pass


def test_model_endpoint_crud_validate_and_default_selection(api_client):
    """Create, rename, validate, promote, restore, and delete an LLM endpoint."""
    suffix = uuid.uuid4().hex[:8]
    endpoint_name = f"ci-llm-{suffix}"
    endpoint_renamed = f"{endpoint_name}-renamed"
    original_default_llm = None

    try:
        openapi = api_client.get("/openapi.json")
        _assert_success(openapi, context="openapi")
        assert "/model-endpoints/" in openapi.json()["paths"]

        endpoints = api_client.get("/model-endpoints/", params={"model_type": "llm"})
        _assert_success(endpoints, context="list llm endpoints")
        original_default_llm = next(row["name"] for row in endpoints.json() if row["is_default"])

        create_endpoint = api_client.post(
            "/model-endpoints/",
            json={
                "name": endpoint_name,
                "model_type": "llm",
                "endpoint": MOCK_VLLM_ENDPOINT,
                "model_name": f"missing-model-{suffix}",
                "timeout": 5,
                "extra": {"implementation": "vllm", "api_key": "ci-test-key"},
            },
        )
        assert create_endpoint.status_code == 201, create_endpoint.text
        assert "ci-test-key" not in create_endpoint.text
        assert create_endpoint.json()["has_api_key"] is True
        assert create_endpoint.json()["extra"]["api_key"] == "ci-********"

        validate_missing = api_client.post(f"/model-endpoints/llm/{endpoint_name}/validate")
        _assert_success(validate_missing, context="validate missing model")
        missing_probe = validate_missing.json()
        assert missing_probe["reachable"] is True
        assert missing_probe["model_found"] is False
        assert MOCK_CHAT_MODEL in missing_probe["models_served"]

        rename_endpoint = api_client.put(
            f"/model-endpoints/llm/{endpoint_name}",
            json={"name": endpoint_renamed, "model_name": MOCK_CHAT_MODEL},
        )
        _assert_success(rename_endpoint, context="rename endpoint")
        assert rename_endpoint.json()["name"] == endpoint_renamed

        validate_real = api_client.post(f"/model-endpoints/llm/{endpoint_renamed}/validate")
        _assert_success(validate_real, context="validate mock model")
        real_probe = validate_real.json()
        assert real_probe["reachable"] is True
        assert real_probe["model_found"] is True

        set_default = api_client.post(f"/model-endpoints/llm/{endpoint_renamed}/set-default")
        _assert_success(set_default, context="set endpoint default")
        assert set_default.json()["is_default"] is True

        restore_default = api_client.post(f"/model-endpoints/llm/{original_default_llm}/set-default")
        _assert_success(restore_default, context="restore original default endpoint")
        assert restore_default.json()["is_default"] is True
    finally:
        if original_default_llm is not None:
            api_client.post(f"/model-endpoints/llm/{original_default_llm}/set-default")
        _delete_ignore_errors(api_client, f"/model-endpoints/llm/{endpoint_renamed}")
        _delete_ignore_errors(api_client, f"/model-endpoints/llm/{endpoint_name}")


def test_update_is_default_keeps_single_default(api_client):
    """PUT {"is_default": true} via the update path must not create a second default.

    Regression: ``update`` ran a bare ``SET is_default = true`` without clearing the
    previous default, so a single PUT left two is_default=true rows for one type and
    the 'default' alias resolved to whichever sorted last by name. The update path now
    routes ``is_default`` through ``set_default`` (atomic clear-then-set).
    """
    suffix = uuid.uuid4().hex[:8]
    name_a = f"ci-llm-a-{suffix}"
    name_b = f"ci-llm-b-{suffix}"
    original_default_llm = None

    try:
        endpoints = api_client.get("/model-endpoints/", params={"model_type": "llm"})
        _assert_success(endpoints, context="list llm endpoints")
        original_default_llm = next(row["name"] for row in endpoints.json() if row["is_default"])

        for name in (name_a, name_b):
            created = api_client.post(
                "/model-endpoints/",
                json={
                    "name": name,
                    "model_type": "llm",
                    "endpoint": MOCK_VLLM_ENDPOINT,
                    "model_name": MOCK_CHAT_MODEL,
                },
            )
            assert created.status_code == 201, created.text
            assert created.json()["is_default"] is False

        # Promote A through the dedicated endpoint, then promote B through the UPDATE
        # path. The second promotion must demote A, not add a parallel default.
        _assert_success(api_client.post(f"/model-endpoints/llm/{name_a}/set-default"), context="set-default A")

        promote_b = api_client.put(f"/model-endpoints/llm/{name_b}", json={"is_default": True})
        _assert_success(promote_b, context="update B is_default=true")
        assert promote_b.json()["is_default"] is True

        listing = api_client.get("/model-endpoints/", params={"model_type": "llm"}).json()
        defaults = sorted(row["name"] for row in listing if row["is_default"])
        assert defaults == [name_b], f"expected exactly one default ({name_b}), got {defaults}"
    finally:
        if original_default_llm is not None:
            api_client.post(f"/model-endpoints/llm/{original_default_llm}/set-default")
        _delete_ignore_errors(api_client, f"/model-endpoints/llm/{name_a}")
        _delete_ignore_errors(api_client, f"/model-endpoints/llm/{name_b}")


def test_create_is_default_keeps_single_default(api_client):
    """POST {"is_default": true} must not add a second default for the type.

    Regression (Hedi's review): "A create request marked as default can still add
    another default without demoting the current one." The create path ran a bare
    INSERT with the is_default flag straight through — the same hazard the update
    path avoids — so registering a new endpoint marked default while one already
    existed left two is_default=true rows. create now demotes any existing default
    in the same transaction, so the new endpoint becomes the sole default.
    """
    suffix = uuid.uuid4().hex[:8]
    new_name = f"ci-llm-create-default-{suffix}"
    original_default_llm = None

    try:
        endpoints = api_client.get("/model-endpoints/", params={"model_type": "llm"})
        _assert_success(endpoints, context="list llm endpoints")
        original_default_llm = next(row["name"] for row in endpoints.json() if row["is_default"])
        assert original_default_llm != new_name

        created = api_client.post(
            "/model-endpoints/",
            json={
                "name": new_name,
                "model_type": "llm",
                "endpoint": MOCK_VLLM_ENDPOINT,
                "model_name": MOCK_CHAT_MODEL,
                "is_default": True,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["is_default"] is True

        listing = api_client.get("/model-endpoints/", params={"model_type": "llm"}).json()
        defaults = sorted(row["name"] for row in listing if row["is_default"])
        assert defaults == [new_name], f"expected exactly one default ({new_name}), got {defaults}"
    finally:
        if original_default_llm is not None:
            restore = api_client.post(f"/model-endpoints/llm/{original_default_llm}/set-default")
            _assert_success(restore, context=f"restore default llm endpoint {original_default_llm}")
        _delete_ignore_errors(api_client, f"/model-endpoints/llm/{new_name}")


def test_set_default_missing_target_keeps_existing_default(api_client):
    """set-default on a vanished target must never leave the type with no default.

    Regression (Hedi's review): "set-default checks the target before the
    transaction that clears/promotes defaults. If the target disappears in that
    gap, the model type can end up with no default." The promote transaction now
    verifies the target via ``RETURNING`` and rolls the clear back when it matched
    no row, so the previous default survives. At the API level the observable
    contract is: a set-default whose target does not exist is a clean 404 that
    leaves exactly one default standing — never zero. (The repo-level rollback on
    the concurrent-delete race is covered deterministically by the unit suite.)
    """
    endpoints = api_client.get("/model-endpoints/", params={"model_type": "llm"})
    _assert_success(endpoints, context="list llm endpoints")
    original_default_llm = next(row["name"] for row in endpoints.json() if row["is_default"])

    missing = f"ci-llm-ghost-{uuid.uuid4().hex[:8]}"
    promote_missing = api_client.post(f"/model-endpoints/llm/{missing}/set-default")
    assert promote_missing.status_code == 404, promote_missing.text

    listing = api_client.get("/model-endpoints/", params={"model_type": "llm"}).json()
    defaults = sorted(row["name"] for row in listing if row["is_default"])
    assert defaults == [original_default_llm], f"expected the original default to survive, got {defaults}"
