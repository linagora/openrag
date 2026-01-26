"""
Pytest fixtures for OpenRAG API tests.
"""

import os
import uuid

import httpx
import pytest

API_BASE_URL = os.environ.get("OPENRAG_API_URL", "http://localhost:8080")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN")


def is_auth_enabled():
    """Check if authentication is enabled."""
    return AUTH_TOKEN is not None and AUTH_TOKEN != "none"


@pytest.fixture(scope="session")
def api_client():
    """Create HTTP client for API tests (uses admin token if AUTH_TOKEN is set)."""
    headers = {}
    if is_auth_enabled():
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0, headers=headers) as client:
        yield client


@pytest.fixture
def created_user(api_client):
    """Create a regular user and return their info, clean up after test."""
    if not is_auth_enabled():
        pytest.skip("Authentication is disabled")

    display_name = f"test-user-{uuid.uuid4().hex[:8]}"
    external_id = f"ext-{uuid.uuid4().hex[:8]}"

    response = api_client.post(
        "/users/",
        data={"display_name": display_name, "external_user_id": external_id},
    )
    assert response.status_code == 201, f"Failed to create user: {response.text}"
    user = response.json()

    yield user

    # Cleanup
    try:
        api_client.delete(f"/users/{user['id']}")
    except Exception:
        pass


@pytest.fixture
def user_client(created_user):
    """Create HTTP client authenticated as the created regular user."""
    headers = {"Authorization": f"Bearer {created_user['token']}"}
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0, headers=headers) as client:
        yield client


@pytest.fixture
def sample_text_file(tmp_path):
    """Create a sample text file for upload tests."""
    content = """This is a test document about artificial intelligence and machine learning.

Machine learning is a subset of artificial intelligence that enables systems to learn
and improve from experience without being explicitly programmed.

Deep learning is a type of machine learning based on artificial neural networks.
It has revolutionized fields like computer vision and natural language processing.

This document is used for testing the OpenRAG API file indexing capabilities.
"""
    file_path = tmp_path / "test_doc.txt"
    file_path.write_text(content)
    return file_path


@pytest.fixture
def sample_markdown_file(tmp_path):
    """Create a sample markdown file for upload tests."""
    content = """# Test Document

## Introduction

This is a **markdown** document for testing purposes.

## Content

- Item 1: Testing file upload
- Item 2: Testing indexing
- Item 3: Testing search

## Conclusion

This concludes our test document.
"""
    file_path = tmp_path / "test_doc.md"
    file_path.write_text(content)
    return file_path


@pytest.fixture
def test_partition_name():
    """Generate unique partition name for test isolation."""
    return f"test-partition-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def created_partition(api_client, test_partition_name):
    """Create a partition and clean it up after the test."""
    response = api_client.post(f"/partition/{test_partition_name}")
    assert response.status_code in [200, 201], f"Failed to create partition: {response.text}"
    yield test_partition_name
    # Cleanup
    try:
        api_client.delete(f"/partition/{test_partition_name}")
    except Exception:
        pass


@pytest.fixture
def sample_markdown_with_image(tmp_path):
    """Create markdown file with embedded data URI image."""
    # Small 1x1 red PNG as data URI
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
    content = f"# Test Document\n\n![sample image]({data_uri})\n\nTest content."
    file_path = tmp_path / "test_with_image.md"
    file_path.write_text(content)
    return file_path
