"""Preset admin API integration tests."""

import uuid


def _assert_success(response, *, context: str) -> None:
    assert 200 <= response.status_code < 300, f"{context}: {response.status_code} {response.text}"


def _delete_ignore_errors(api_client, path: str) -> None:
    try:
        api_client.delete(path)
    except Exception:
        pass


def _indexation_config(chunk_size: int) -> dict:
    return {
        "chunking": {
            "name": "recursive_splitter",
            "chunk_size": chunk_size,
            "chunk_overlap_rate": 0.1,
        },
        "parsing_strategy": "pymupdf",
        "enable_image_captioning": False,
        "enable_contextualization": False,
        "enable_metadata_extraction": False,
        "enable_entity_extraction": False,
        "enable_topic_tagging": False,
    }


def _retrieval_config(top_n: int = 3) -> dict:
    return {
        "type": "single",
        "top_k": 5,
        "top_n": top_n,
        "similarity_threshold": 0.0,
        "enable_reranker": False,
        "include_related": False,
        "include_ancestors": False,
    }


def test_preset_options_crud_and_rename(api_client):
    """Create, update, rename, list, and delete indexation/retrieval presets."""
    suffix = uuid.uuid4().hex[:8]
    indexation_preset = f"ci-index-{suffix}"
    retrieval_preset = f"ci-retrieval-{suffix}"
    retrieval_preset_renamed = f"{retrieval_preset}-renamed"

    try:
        openapi = api_client.get("/openapi.json")
        _assert_success(openapi, context="openapi")
        paths = openapi.json()["paths"]
        assert "/presets/" in paths
        assert "/presets/options" in paths

        options = api_client.get("/presets/options")
        _assert_success(options, context="preset options")
        option_data = options.json()
        assert "recursive_splitter" in option_data["chunking_strategies"]
        assert "single" in option_data["retrieval_types"]
        # Parsing strategies are derived from IndexationPipelineConfig's Literal.
        assert set(option_data["parsing_strategies"]) == {"pymupdf", "marker", "docling"}

        create_indexation = api_client.post(
            "/presets/",
            json={
                "name": indexation_preset,
                "preset_type": "indexation",
                "config": _indexation_config(64),
            },
        )
        assert create_indexation.status_code == 201, create_indexation.text

        update_indexation = api_client.put(
            f"/presets/indexation/{indexation_preset}",
            json={"config": _indexation_config(96)},
        )
        _assert_success(update_indexation, context="update indexation preset")
        assert update_indexation.json()["config"]["chunking"]["chunk_size"] == 96

        create_retrieval = api_client.post(
            "/presets/",
            json={
                "name": retrieval_preset,
                "preset_type": "retrieval",
                "config": _retrieval_config(),
            },
        )
        assert create_retrieval.status_code == 201, create_retrieval.text

        rename_retrieval = api_client.put(
            f"/presets/retrieval/{retrieval_preset}",
            json={"name": retrieval_preset_renamed},
        )
        _assert_success(rename_retrieval, context="rename retrieval preset")
        assert rename_retrieval.json()["name"] == retrieval_preset_renamed

        list_retrieval = api_client.get("/presets/", params={"preset_type": "retrieval"})
        _assert_success(list_retrieval, context="list retrieval presets")
        rows_by_name = {row["name"]: row for row in list_retrieval.json()}
        assert retrieval_preset_renamed in rows_by_name
        # Freshly created preset with no partitions pointing at it yet.
        assert rows_by_name[retrieval_preset_renamed]["used_by_partitions"] == 0

        get_retrieval = api_client.get(f"/presets/retrieval/{retrieval_preset_renamed}")
        _assert_success(get_retrieval, context="get renamed retrieval preset")
        assert get_retrieval.json()["config"]["top_n"] == 3
    finally:
        _delete_ignore_errors(api_client, f"/presets/indexation/{indexation_preset}")
        _delete_ignore_errors(api_client, f"/presets/retrieval/{retrieval_preset_renamed}")
        _delete_ignore_errors(api_client, f"/presets/retrieval/{retrieval_preset}")
