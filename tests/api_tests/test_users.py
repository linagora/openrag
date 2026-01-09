"""User management API tests."""
import pytest


class TestUserManagement:
    """Test user CRUD operations."""

    def test_get_current_user(self, api_client):
        """Test getting current user info."""
        response = api_client.get("/users/info")
        assert response.status_code == 200
        data = response.json()
        assert "id" in data

    def test_list_users(self, api_client):
        """Test listing users."""
        response = api_client.get("/users/")
        # Without auth token, may return default user or all users
        assert response.status_code == 200
        data = response.json()
        assert "users" in data

    def test_get_user_by_id(self, api_client):
        """Test getting user by ID."""
        # First get current user to know a valid ID
        info_response = api_client.get("/users/info")
        user_id = info_response.json().get("id", 1)

        response = api_client.get(f"/users/{user_id}")
        assert response.status_code == 200

    def test_get_nonexistent_user(self, api_client):
        """Test getting non-existent user."""
        response = api_client.get("/users/99999")
        assert response.status_code == 404
