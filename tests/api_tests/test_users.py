"""User management API tests."""


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

    def test_update_user_quota(self, api_client):
        """Test updating user quota - create user with quota 10, then update to 12."""
        # Create a new user with quota 10
        create_response = api_client.post(
            "/users/",
            data={"display_name": "quota_test_user", "file_quota": 10},
        )
        assert create_response.status_code == 201
        user_data = create_response.json()
        user_id = user_data["id"]

        try:
            # Update the user's quota to 12
            update_response = api_client.patch(
                f"/users/{user_id}/quota",
                data={"file_quota": 12},
            )
            assert update_response.status_code == 200
            update_data = update_response.json()
            assert "message" in update_data
            assert str(user_id) in update_data["message"]
            assert "12" in update_data["message"]

            # check that user user has quota 12
            get_response = api_client.get(f"/users/{user_id}")
            assert get_response.status_code == 200
            get_data = get_response.json()
            assert get_data["file_quota"] == 12, "User quota was not updated correctly."
        finally:
            # Clean up: delete the test user
            api_client.delete(f"/users/{user_id}")

    def test_user_default_quota(self, api_client):
        """Test that user created with None quota gets default value (10)."""
        # Create a user without specifying quota (None)
        create_response = api_client.post(
            "/users/",
            data={"display_name": "default_quota_user"},
        )
        assert create_response.status_code == 201
        user_data = create_response.json()
        user_id = user_data["id"]

        try:
            # Verify the user has the default quota (10)
            get_response = api_client.get(f"/users/{user_id}")
            assert get_response.status_code == 200
            get_data = get_response.json()
            assert get_data["file_quota"] == 10, "User should have default quota of 10"
        finally:
            # Clean up: delete the test user
            api_client.delete(f"/users/{user_id}")
