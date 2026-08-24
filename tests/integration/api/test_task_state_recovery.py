"""Task state recovery API tests."""


def _task_state_actor_id(api_client) -> str:
    response = api_client.get("/actors/")
    assert response.status_code == 200

    actor = next(
        actor
        for actor in response.json()["actors"]
        if actor["name"] == "TaskStateManager" and actor["state"] == "ALIVE"
    )
    return actor["actor_id"]


def test_cached_job_service_recovers_after_actor_process_restart(api_client):
    queue_before = api_client.get("/queue/info")
    assert queue_before.status_code == 200
    actor_id = _task_state_actor_id(api_client)

    restart = api_client.post("/actors/TaskStateManager/restart")
    assert restart.status_code == 200
    assert restart.json()["actor_id"] == actor_id

    queue_after = api_client.get("/queue/info")
    assert queue_after.status_code == 200
    assert "tasks" in queue_after.json()
