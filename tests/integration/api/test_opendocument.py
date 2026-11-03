"""ODT and ODP files through the running API, from upload to search (#1287)."""

from pathlib import Path

import pytest
from conftest import API_BASE_URL, wait_for_task

RESOURCES_DIR = Path(__file__).parents[2] / "resources"


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("test_file.odt", "application/vnd.oasis.opendocument.text", "Rapport annuel"),
        ("test_file.odp", "application/vnd.oasis.opendocument.presentation", "Bilan 2026"),
    ],
)
def test_an_uploaded_file_can_be_found(api_client, created_partition, filename, content_type, expected):
    with open(RESOURCES_DIR / filename, "rb") as f:
        response = api_client.post(
            f"/indexer/partition/{created_partition}/file/{filename.replace('.', '-')}",
            files={"file": (filename, f, content_type)},
            data={"metadata": "{}"},
        )
    assert response.status_code in [200, 201, 202], response.text
    wait_for_task(api_client, response.json()["task_status_url"].split("/")[-1])

    search = api_client.get("/search", params={"partitions": created_partition, "text": expected, "top_k": 5})
    assert search.status_code == 200, search.text
    links = [document["link"].removeprefix(API_BASE_URL) for document in search.json()["documents"]]
    assert any(expected in api_client.get(link).text for link in links), f"{expected!r} not in {links}"


@pytest.mark.parametrize("filename", ["renamed.odt", "renamed.odp"])
def test_a_zip_that_is_not_opendocument_is_refused(api_client, created_partition, filename):
    empty_zip = b"PK\x05\x06" + b"\x00" * 18
    response = api_client.post(
        f"/indexer/partition/{created_partition}/file/not-opendocument",
        files={"file": (filename, empty_zip)},
        data={"metadata": "{}"},
    )

    assert response.status_code == 415, response.text
