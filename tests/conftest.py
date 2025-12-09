# tests/conftest.py
import os
import time
import httpx
import pytest


@pytest.fixture(scope="session")
def base_url():
    return os.environ.get("RAG_BASE_URL", "http://localhost:8080")


@pytest.fixture(scope="session", autouse=True)
def wait_for_api(base_url):
    timeout = 60
    start = time.time()

    while True:
        try:
            r = httpx.get(f"{base_url}/health_check", timeout=3)
            if r.status_code == 200:
                return
        except Exception:
            pass

        if time.time() - start > timeout:
            raise RuntimeError("API not ready")

        time.sleep(2)
