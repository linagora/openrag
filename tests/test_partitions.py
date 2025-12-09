# tests/test_partition.py
import httpx
import uuid


def test_create_partition(base_url):
    client = httpx.Client(timeout=5)

    partition_name = "tets-partition"
    r = client.post(f"{base_url}/partition/{partition_name}")
    assert r.status_code is 200

