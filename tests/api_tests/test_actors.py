"""Ray actors API tests."""
import pytest


class TestActorsAPI:
    """Test Ray actors management endpoints."""

    def test_list_actors(self, api_client):
        """Test listing Ray actors."""
        # Use trailing slash to avoid redirect
        response = api_client.get("/actors/")
        # May require admin privileges or return redirect
        assert response.status_code in [200, 307, 403]

        if response.status_code == 200:
            data = response.json()
            # Should return actor information
            assert isinstance(data, (list, dict))
