"""Keep a pool's child processes from outliving the process that started them.

A ``ProcessPoolExecutor`` child is not stopped when its parent dies abruptly — an
OOM kill, ``kill -9``, a crash. It is re-parented, keeps its memory (a Marker
child also keeps its GPU memory), and waits forever on a queue no one writes to
any more (#1167). Ray stops a worker's children when it stops the worker itself
(``ray.kill``), but not after such a death.

:func:`tie_to_parent` runs first in each child. :func:`run_tied` does the same for
the processes a child starts in turn, such as pdftext's own process pool inside a
Marker child, without moving them out of the child's group. :func:`kill_process_group`
is how the parent reclaims a child together with whatever the child started, and
:func:`kill_orphaned_group` reclaims what a child that already died left behind.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from collections.abc import Callable

from core.utils.logging import get_logger

logger = get_logger()

# From <linux/prctl.h>.
_PR_SET_PDEATHSIG = 1


def tie_to_parent(parent_pid: int) -> None:
    """Make this child die with its parent and lead its own process group.

    Call it first in a pool initializer, with the parent's pid read in the
    parent (``os.getpid()`` when building the pool).

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
    die_with_parent(parent_pid)


def die_with_parent(parent_pid: int) -> None:
    """Have the kernel ``SIGKILL`` this process when its parent dies.

    :func:`tie_to_parent` without the new process group: the process stays in
    its parent's group, so killing that group still reaches it. The same thread
    rule applies, and a process whose parent is already gone exits at once.
    Linux only, and best-effort.
    """
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


def run_tied(parent_pid: int, initializer: Callable[..., object] | None, initargs: tuple) -> None:
    """Pool initializer: :func:`die_with_parent`, then the pool's own *initializer*.

    For a pool whose initializer someone else chose: pass this one instead, with
    the pid of the process building the pool and the original initializer.
    """
    die_with_parent(parent_pid)
    if initializer is not None:
        initializer(*initargs)


def kill_process_group(pid: int) -> None:
    """``SIGKILL`` the process group led by *pid*, or *pid* alone if it leads none.

    A child killed before its initializer ran :func:`tie_to_parent` leads no group
    yet, and is killed by itself. Linux does not reuse a pid while a group still
    carries it, so the group killed is always this child's.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def kill_orphaned_group(pid: int) -> None:
    """``SIGKILL`` what is left of the process group a reaped child led.

    Linux does not give a pid to a new process while any process still carries it
    as a group id. So if a process with *pid* exists, the group is gone and the pid
    now belongs to an unrelated process, which is left alone. Otherwise every
    process in group *pid* was started by that child. The one exposure is the pid
    being reused in the few microseconds between the two calls.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError:
        return
    else:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
