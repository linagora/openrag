"""
Pytest fixtures for OpenRAG API tests.
"""

import os
import time
import uuid

import httpx
import pytest

API_BASE_URL = os.environ.get("OPENRAG_API_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def api_client():
    """Create HTTP client for API tests."""
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def wait_for_api():
    """Wait for OpenRAG API to be ready."""
    max_retries = 60
    for i in range(max_retries):
        try:
            response = httpx.get(f"{API_BASE_URL}/health_check", timeout=5.0)
            if response.status_code == 200:
                print(f"API ready after {i + 1} attempts")
                return
        except httpx.RequestError:
            pass
        time.sleep(2)
    pytest.fail(f"API not ready after {max_retries * 2} seconds")


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
