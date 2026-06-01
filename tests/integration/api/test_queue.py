"""Queue and task management API tests."""


class TestQueueManagement:
    """Test queue and task operations."""

    def test_list_tasks(self, api_client):
        """Test listing tasks."""
        response = api_client.get("/queue/tasks")
        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data

    def test_list_active_tasks(self, api_client):
        """Test listing active tasks."""
        response = api_client.get("/queue/tasks", params={"task_status": "active"})
        assert response.status_code == 200

    def test_list_completed_tasks(self, api_client):
        """Test listing completed tasks."""
        response = api_client.get("/queue/tasks", params={"task_status": "completed"})
        assert response.status_code == 200

    def test_queue_info(self, api_client):
        """Test getting queue info."""
        response = api_client.get("/queue/info")
        # May require admin privileges
        assert response.status_code in [200, 403]
