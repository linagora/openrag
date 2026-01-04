"""Tools API tests."""
import pytest


class TestToolsAPI:
    """Test tools endpoint functionality."""

    def test_list_tools(self, api_client):
        """Test listing available tools."""
        response = api_client.get("/v1/tools")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Check extractText tool exists
        tool_names = [t["name"] for t in data]
        assert "extractText" in tool_names

    def test_tool_has_required_fields(self, api_client):
        """Test that tools have required fields."""
        response = api_client.get("/v1/tools")
        data = response.json()

        for tool in data:
            assert "name" in tool
            assert "description" in tool
