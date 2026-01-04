"""Health check endpoint tests."""


def test_health_check(api_client):
    """Test health check endpoint returns OK."""
    response = api_client.get("/health_check")
    assert response.status_code == 200
    assert "RAG API is up" in response.text


def test_openapi_docs_accessible(api_client):
    """Test OpenAPI documentation is accessible."""
    response = api_client.get("/docs")
    assert response.status_code == 200


def test_openapi_json_accessible(api_client):
    """Test OpenAPI JSON schema is accessible."""
    response = api_client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert "openapi" in data
    assert "paths" in data
