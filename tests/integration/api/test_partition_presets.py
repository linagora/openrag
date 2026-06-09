"""Partition preset assignment integration tests."""

import uuid

from conftest import wait_for_indexing


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


def _retrieval_config() -> dict:
    return {
        "type": "single",
        "top_k": 5,
        "top_n": 3,
        "similarity_threshold": 0.0,
        "enable_reranker": False,
        "include_related": False,
        "include_ancestors": False,
    }


def test_partition_preset_assignment_drives_indexing_and_search(api_client, tmp_path):
    """Assign presets to a partition, index a file, and search the resolved partition."""
    suffix = uuid.uuid4().hex[:8]
    indexation_preset = f"ci-index-{suffix}"
    retrieval_preset = f"ci-retrieval-{suffix}"
    partition = f"ci-partition-{suffix}"
    file_id = f"ci-file-{suffix}"
    marker = f"ci-marker-{suffix}"

    try:
        openapi = api_client.get("/openapi.json")
        _assert_success(openapi, context="openapi")
        assert "/partition/{partition}/config" in openapi.json()["paths"]

        create_indexation = api_client.post(
            "/presets/",
            json={
                "name": indexation_preset,
                "preset_type": "indexation",
                "config": _indexation_config(96),
            },
        )
        assert create_indexation.status_code == 201, create_indexation.text

        create_retrieval = api_client.post(
            "/presets/",
            json={
                "name": retrieval_preset,
                "preset_type": "retrieval",
                "config": _retrieval_config(),
            },
        )
        assert create_retrieval.status_code == 201, create_retrieval.text

        create_partition = api_client.post(f"/partition/{partition}")
        assert create_partition.status_code == 201, create_partition.text

        update_partition = api_client.patch(
            f"/partition/{partition}",
            json={
                "indexation_preset": indexation_preset,
                "retrieval_preset": retrieval_preset,
            },
        )
        _assert_success(update_partition, context="assign partition presets")
        partition_config = update_partition.json()
        assert partition_config["indexation_preset"] == indexation_preset
        assert partition_config["retrieval_preset"] == retrieval_preset
        assert partition_config["indexation_pipeline"]["chunking"]["chunk_size"] == 96
        assert partition_config["retrieval_pipeline"]["top_n"] == 3

        invalid_preset = api_client.patch(
            f"/partition/{partition}",
            json={"indexation_preset": f"missing-indexation-{suffix}"},
        )
        assert invalid_preset.status_code == 422
        assert "PRESET_NOT_FOUND" in invalid_preset.text

        test_file = tmp_path / "preset-indexing.txt"
        test_file.write_text(
            f"Preset assignment CI document. Unique marker: {marker}. "
            "This file verifies preset-based indexing and search."
        )

        with test_file.open("rb") as handle:
            upload = api_client.post(
                f"/indexer/partition/{partition}/file/{file_id}",
                files={"file": ("preset-indexing.txt", handle, "text/plain")},
                data={"metadata": f'{{"source":"preset-ci","marker":"{marker}"}}'},
            )
        assert upload.status_code in {200, 201, 202}, upload.text
        wait_for_indexing(api_client, upload.json())

        indexed_file = api_client.get(f"/partition/{partition}/file/{file_id}")
        _assert_success(indexed_file, context="get indexed file")
        assert len(indexed_file.json()["documents"]) > 0

        search = api_client.get(
            f"/search/partition/{partition}",
            params={"text": marker, "top_k": 3, "similarity_threshold": 0.0},
        )
        _assert_success(search, context="search indexed file")
        documents = search.json()["documents"]
        assert documents
        assert any(doc["metadata"].get("file_id") == file_id for doc in documents)
    finally:
        _delete_ignore_errors(api_client, f"/indexer/partition/{partition}/file/{file_id}")
        _delete_ignore_errors(api_client, f"/partition/{partition}")
        _delete_ignore_errors(api_client, f"/presets/indexation/{indexation_preset}")
        _delete_ignore_errors(api_client, f"/presets/retrieval/{retrieval_preset}")
