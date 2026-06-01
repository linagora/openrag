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
            json={"display_name": "quota_test_user", "file_quota": 10},
        )
        assert create_response.status_code == 201
        user_data = create_response.json()
        user_id = user_data["id"]

        try:
            # Update the user's quota to 12
            update_response = api_client.patch(
                f"/users/{user_id}",
                json={"file_quota": 12},
            )
            assert update_response.status_code == 200
            update_data = update_response.json()
            assert update_data["file_quota"] == 12

            # check that user has quota 12
            get_response = api_client.get(f"/users/{user_id}")
            assert get_response.status_code == 200
            get_data = get_response.json()
            assert get_data["file_quota"] == 12, "User quota was not updated correctly."
        finally:
            # Clean up: delete the test user
            api_client.delete(f"/users/{user_id}")

    def test_update_user_display_name(self, api_client):
        """Test updating user display_name."""
        create_response = api_client.post(
            "/users/",
            json={"display_name": "original_name"},
        )
        assert create_response.status_code == 201
        user_id = create_response.json()["id"]

        try:
            update_response = api_client.patch(
                f"/users/{user_id}",
                json={"display_name": "updated_name"},
            )
            assert update_response.status_code == 200
            assert update_response.json()["display_name"] == "updated_name"

            # Verify persistence
            get_response = api_client.get(f"/users/{user_id}")
            assert get_response.json()["display_name"] == "updated_name"
        finally:
            api_client.delete(f"/users/{user_id}")

    def test_update_user_multiple_fields(self, api_client):
        """Test updating multiple user fields at once."""
        create_response = api_client.post(
            "/users/",
            json={"display_name": "multi_test", "file_quota": 5},
        )
        assert create_response.status_code == 201
        user_id = create_response.json()["id"]

        try:
            update_response = api_client.patch(
                f"/users/{user_id}",
                json={
                    "display_name": "multi_updated",
                    "external_user_id": "ext-123",
                    "file_quota": 20,
                },
            )
            assert update_response.status_code == 200
            data = update_response.json()
            assert data["display_name"] == "multi_updated"
            assert data["external_user_id"] == "ext-123"
            assert data["file_quota"] == 20
        finally:
            api_client.delete(f"/users/{user_id}")

    def test_update_user_partial_preserves_other_fields(self, api_client):
        """Test that partial update preserves fields not in request."""
        create_response = api_client.post(
            "/users/",
            json={
                "display_name": "partial_test",
                "external_user_id": "ext-preserve",
                "file_quota": 15,
            },
        )
        assert create_response.status_code == 201
        user_id = create_response.json()["id"]

        try:
            # Update only display_name
            update_response = api_client.patch(
                f"/users/{user_id}",
                json={"display_name": "partial_updated"},
            )
            assert update_response.status_code == 200
            data = update_response.json()
            # Updated field should change
            assert data["display_name"] == "partial_updated"
            # Other fields should be preserved
            assert data["external_user_id"] == "ext-preserve"
            assert data["file_quota"] == 15
        finally:
            api_client.delete(f"/users/{user_id}")

    def test_update_user_is_admin(self, api_client):
        """Test granting and revoking admin privileges."""
        create_response = api_client.post(
            "/users/",
            json={"display_name": "admin_test", "is_admin": False},
        )
        assert create_response.status_code == 201
        user_id = create_response.json()["id"]

        try:
            # Grant admin
            update_response = api_client.patch(
                f"/users/{user_id}",
                json={"is_admin": True},
            )
            assert update_response.status_code == 200
            assert update_response.json()["is_admin"] is True

            # Revoke admin
            update_response = api_client.patch(
                f"/users/{user_id}",
                json={"is_admin": False},
            )
            assert update_response.status_code == 200
            assert update_response.json()["is_admin"] is False
        finally:
            api_client.delete(f"/users/{user_id}")

    def test_cannot_revoke_admin_from_default_user(self, api_client):
        """Test that admin cannot be revoked from user 1 (default admin)."""
        # Attempt to revoke admin from user 1
        response = api_client.patch(
            "/users/1",
            json={"is_admin": False},
        )
        assert response.status_code == 400
        assert "Cannot revoke admin" in response.json()["detail"]

    def test_can_update_other_fields_on_default_user(self, api_client):
        """Test that other fields can be updated on user 1 (default admin)."""
        # Get current display_name
        get_response = api_client.get("/users/1")
        assert get_response.status_code == 200
        original_name = get_response.json().get("display_name")

        try:
            # Update display_name (not is_admin)
            response = api_client.patch(
                "/users/1",
                json={"display_name": "updated_admin_name"},
            )
            assert response.status_code == 200
            assert response.json()["display_name"] == "updated_admin_name"
        finally:
            # Restore original name
            restore_response = api_client.patch("/users/1", json={"display_name": original_name})
            assert restore_response.status_code == 200

    def test_user_default_quota(self, api_client):
        """Test that user created with None quota gets default value (10)."""
        # Create a user without specifying quota (None)
        create_response = api_client.post(
            "/users/",
            json={"display_name": "default_quota_user"},
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
