"""Keep a pool's child processes from outliving the process that started them.

A ``ProcessPoolExecutor`` child is not stopped when its parent dies abruptly — an
OOM kill, ``kill -9``, a crash. It is re-parented, keeps its memory (a Marker
child also keeps its GPU memory), and waits forever on a queue no one writes to
any more (#1167). Ray stops a worker's children when it stops the worker itself
(``ray.kill``), but not after such a death.

:func:`tie_to_parent` runs first in each child. :func:`kill_process_group` is how
the parent reclaims a child together with whatever the child started.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys

from core.utils.logging import get_logger

logger = get_logger()

# From <linux/prctl.h>.
_PR_SET_PDEATHSIG = 1


def tie_to_parent(parent_pid: int) -> None:
    """Make this child die with its parent and lead its own process group.

    Call it first in a pool initializer, with the parent's pid read in the
    parent (``os.getpid()`` when building the pool). The pool must use the
    ``spawn`` or ``fork`` start method: under ``forkserver`` the child's parent
    is the fork server, so every child would exit at once.

    - The kernel sends this child ``SIGKILL`` when its parent dies
      (``PR_SET_PDEATHSIG``, Linux only). It fires when the *thread* that
      started the child exits, not the process: a child started by a
      ``submit`` from a short-lived thread is killed when that thread ends, and
      the pool breaks. Submit from a thread that lives as long as the pool.
    - If the parent died before the signal was set, nothing would deliver it, so
      the child exits at once.
    - As a group leader, everything the child starts joins its group, so
      :func:`kill_process_group` reaches it. The signal itself is not
      inherited: the child's own children are not killed with the parent.

    Best-effort: a platform without ``prctl`` or a refused call leaves the child
    working, as it did before.
    """
    try:
        os.setpgrp()
    except OSError as exc:
        logger.warning(f"Could not make the pool child a process-group leader: {exc}")
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    except (OSError, AttributeError) as exc:
        logger.warning(f"Could not tie the pool child to its parent: {exc}")
        return
    if os.getppid() != parent_pid:
        os._exit(1)


def kill_process_group(pid: int) -> None:
    """``SIGKILL`` the process group led by *pid*, or *pid* alone if it leads none.

    *pid* must be a child that has not been reaped: until then its pid stays
    taken, so the group or process killed is always this child's. A child killed
    before its initializer ran :func:`tie_to_parent` leads no group yet, and is
    killed by itself.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
