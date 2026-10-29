from types import SimpleNamespace
from unittest.mock import Mock

from services.workers.failure_reporting import submit_task_failure


def test_failure_reporting_keeps_timings_when_only_separate_setters_are_available() -> None:
    reason_setter = Mock()
    timed_setter = Mock()
    actor = SimpleNamespace(
        _ray_actor_method_names={
            "set_failed_with_reason_if_not_cancelled",
            "set_failed_with_stage_timings_if_not_cancelled",
        },
        set_failed_with_reason_if_not_cancelled=reason_setter,
        set_failed_with_stage_timings_if_not_cancelled=timed_setter,
    )
    timings = {"parse": 0.25}

    submit_task_failure(actor, "task-1", "traceback", "parse failed", stage_timings=timings)

    timed_setter.remote.assert_called_once_with("task-1", "traceback", timings)
    reason_setter.remote.assert_not_called()
