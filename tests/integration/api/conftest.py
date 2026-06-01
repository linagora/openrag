"""
Pytest fixtures for OpenRAG API tests.
"""

import json
import os
import time
import uuid
from pathlib import Path

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
    with httpx.Client(base_url=API_BASE_URL, timeout=60.0, headers=headers) as client:
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
        json={"display_name": display_name, "external_user_id": external_id},
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
    with httpx.Client(base_url=API_BASE_URL, timeout=60.0, headers=headers) as client:
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


TASK_TIMEOUT = 180  # 3 minutes for file processing


def wait_for_task(
    api_client,
    task_id: str,
    timeout: int = TASK_TIMEOUT,
    headers: dict | None = None,
) -> dict:
    """Wait for task completion, polling status endpoint.

    Handles 404 responses gracefully as task may not be registered yet.
    """
    start = time.time()
    while time.time() - start < timeout:
        response = api_client.get(f"/indexer/task/{task_id}", headers=headers)

        # Task might not be registered yet, retry on 404
        if response.status_code == 404:
            time.sleep(0.5)
            continue

        if response.status_code != 200:
            raise AssertionError(f"Task status failed: {response.text}")

        status = response.json()
        state = (status.get("task_state") or "").upper()

        if state in {"COMPLETED", "SUCCESS"}:
            return status
        elif state in {"FAILED", "FAILURE"}:
            raise AssertionError(f"Task failed: {status}")

        time.sleep(1)

    raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")


def wait_for_indexing(api_client, response_data: dict, timeout: int = TASK_TIMEOUT):
    """Wait for file indexing task to complete, extracting task_id from response."""
    if "task_id" in response_data:
        return wait_for_task(api_client, response_data["task_id"], timeout)
    elif "task_status_url" in response_data:
        # Extract task_id from URL like http://host/indexer/task/{task_id}
        task_id = response_data["task_status_url"].split("/")[-1]
        return wait_for_task(api_client, task_id, timeout)
    else:
        time.sleep(5)


@pytest.fixture
def indexed_folder_partition(api_client, created_partition, folder_files):
    """Create partition and index multiple related files with same relationship_id."""

    for filename, (file_path, relationship_id) in folder_files.items():
        file_id = filename.replace(".", "-")

        with open(file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": (filename, f, "text/plain")},
                data={"metadata": f'{{"relationship_id": "{relationship_id}"}}'},
            )

        data = response.json()

        # Wait for each file to be indexed
        if "task_status_url" in data:
            task_url = data["task_status_url"]
            task_path = "/" + "/".join(task_url.split("/")[3:])
        elif "task_id" in data:
            task_path = f"/indexer/task/{data['task_id']}"
        else:
            time.sleep(3)
            continue

        for _ in range(30):
            task_response = api_client.get(task_path)
            task_data = task_response.json()
            state = task_data.get("task_state", "")
            if state in ["SUCCESS", "COMPLETED", "success", "completed"]:
                break
            elif state in ["FAILED", "failed", "FAILURE", "failure"]:
                pytest.skip(f"Indexing failed for {filename}: {task_data}")
            time.sleep(2)

    return created_partition


@pytest.fixture
def sample_markdown_with_image(tmp_path):
    """Create markdown file with embedded data URI image."""
    # Small 1x1 red PNG as data URI
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
    content = f"# Test Document\n\n![sample image]({data_uri})\n\nTest content."
    file_path = tmp_path / "test_with_image.md"
    file_path.write_text(content)
    return file_path


query = """Project Alpha Overview

This document describes the main objectives of Project Alpha.
The project aims to develop a new AI-powered analytics platform.
Key stakeholders include the engineering and product teams.
"""


@pytest.fixture
def folder_files(tmp_path):
    """Create multiple files simulating a folder with related documents."""
    # Create unique content for each file that will be chunked
    relationship_id_1 = "folder1"
    relationship_id_2 = "folder2"
    files = {
        "file1.txt": (query, relationship_id_1),
        "file2.txt": (
            """Project Alpha Technical Specifications

The system will use machine learning models for predictive analytics.
Backend infrastructure includes microservices architecture.
Database: PostgreSQL with vector extensions for embeddings.
""",
            relationship_id_1,
        ),
        "file3.txt": (
            """Project Alpha Timeline

Phase 1: Requirements gathering (Q1 2026)
Phase 2: Development and testing (Q2-Q3 2026)
Phase 3: Deployment and monitoring (Q4 2026)
Expected completion: December 2026.
""",
            relationship_id_1,
        ),
        "file4.txt": ("""Project Beta Overview""", relationship_id_2),
    }

    file_paths = {}
    for filename, (content, relationship_id) in files.items():
        file_path = tmp_path / filename
        file_path.write_text(content)
        file_paths[filename] = (file_path, relationship_id)

    return file_paths


@pytest.fixture
def exact_match_query():
    """Return a query that should exactly match a chunk from folder_files.

    Since embeddings are deterministic (MD5-based) and files are small enough
    to be single chunks, searching with the complete file content should return
    a perfect match.
    """
    # This should exactly match file1.txt as a complete chunk
    return query


@pytest.fixture
def email_thread_files(tmp_path):
    """Load email thread data from JSON and create text files for each email.

    Returns a dict mapping email_id to (file_path, parent_id, relationship_id, subject).
    """
    # Load the email thread JSON
    email_json_path = Path(__file__).parent / "email_test_file.json"
    with open(email_json_path) as f:
        email_data = json.load(f)

    thread_id = email_data["thread_id"]
    emails = email_data["emails"]

    email_files = {}
    for email in emails:
        email_id = email["id"]
        parent_id = email.get("parent_id")
        subject = email["subject"]

        # Create email content
        content = f"""Subject: {subject}
From: {email["from"]}
To: {email["to"]}

{email["body"]}
"""

        # Write to file
        file_path = tmp_path / f"{email_id}.txt"
        file_path.write_text(content)

        email_files[email_id] = {
            "path": file_path,
            "parent_id": parent_id,
            "relationship_id": thread_id,
            "subject": subject,
            "filename": f"{email_id}.txt",
        }

    return email_files
