"""Extract (chunk retrieval) API tests."""
import pytest


class TestExtract:
    """Test chunk extraction functionality."""

    def test_get_nonexistent_extract(self, api_client):
        """Test retrieving non-existent extract returns error."""
        response = api_client.get("/extract/nonexistent-id-12345")
        # May return 404 or 500 depending on implementation
        assert response.status_code in [404, 500]

    def test_extract_invalid_id_format(self, api_client):
        """Test extract with invalid ID format."""
        response = api_client.get("/extract/")
        # Should return method not allowed or not found
        assert response.status_code in [404, 405]
