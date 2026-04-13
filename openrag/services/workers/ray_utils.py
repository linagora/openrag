import asyncio
from typing import Any

import ray
from ray.exceptions import RayTaskError, TaskCancelledError
from utils.logger import get_logger

logger = get_logger()


async def call_ray_actor_with_timeout(
    future: ray.ObjectRef,
    timeout: float,
    task_description: str = "Ray task",
) -> Any:
    """
    Wait for a Ray actor call with timeout and proper cancellation handling.

    This utility provides consistent error handling for Ray actor calls:
    - Timeout with proper task cancellation
    - Propagation of asyncio cancellation to Ray tasks
    - Proper handling of Ray-specific exceptions

    Args:
        future: The Ray ObjectRef from a remote call
        timeout: Timeout in seconds
        task_description: Description for error messages

    Returns:
        The result of the Ray task

    Raises:
        TimeoutError: If the task exceeds the timeout
        asyncio.CancelledError: If the calling coroutine is cancelled
        TaskCancelledError: If the Ray task was cancelled
        RuntimeError: If the Ray task failed with an error
    """
    try:
        result = await asyncio.wait_for(asyncio.gather(future), timeout=timeout)
        return result[0]  # gather returns a list

    except TimeoutError:
        logger.warning(f"{task_description} timed out, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise

    except asyncio.CancelledError:
        logger.warning(f"{task_description} cancelled, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise

    except TaskCancelledError:
        logger.warning(f"{task_description} Ray task was cancelled")
        raise

    except RayTaskError as e:
        raise RuntimeError(f"{task_description} failed") from e
