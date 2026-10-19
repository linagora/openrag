"""Pool children must not outlive the process that started them (#1167).

Run with real processes: what is under test is how the kernel treats them, which
no fake can show.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import signal
import sys
import time

import pytest
from core.utils.child_process import kill_process_group, tie_to_parent

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")

# Kept alive in a pool child, so the processes it started keep running.
_grandchild_pool: concurrent.futures.ProcessPoolExecutor | None = None


def _alive(pid: int) -> bool:
    """Running, and not a zombie waiting for a parent that will never reap it."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        # Gone before the open, or between the open and the read.
        return False


def _alive_after(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return False
        time.sleep(0.1)
    return _alive(pid)


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _pool_parent(tied: bool, child_pids) -> None:
    """Start a spawn pool child as MarkerWorker does, report its pid, then wait."""
    if tied:
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=tie_to_parent,
            initargs=(os.getpid(),),
        )
    else:
        pool = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    child_pids.put(pool.submit(os.getpid).result(timeout=60))
    time.sleep(120)


@linux_only
@pytest.mark.parametrize(("tied", "survives"), [(True, False), (False, True)])
def test_a_pool_child_dies_with_its_killed_parent_only_when_tied(tied, survives):
    """The untied case is the leak: the child outlives a SIGKILLed parent."""
    ctx = multiprocessing.get_context("fork")
    child_pids = ctx.Queue()
    parent = ctx.Process(target=_pool_parent, args=(tied, child_pids))
    parent.start()
    child = child_pids.get(timeout=60)
    try:
        os.kill(parent.pid, signal.SIGKILL)
        parent.join(timeout=10)
        assert _alive_after(child, 2) is survives
    finally:
        _kill_quietly(child)


def _start_grandchildren(count: int) -> list[int]:
    """Start a pool inside this child, as a library used in a parse could."""
    global _grandchild_pool
    _grandchild_pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=count, mp_context=multiprocessing.get_context("spawn")
    )
    for _ in range(count):
        _grandchild_pool.submit(time.sleep, 600)
    deadline = time.monotonic() + 60
    while len(_grandchild_pool._processes) < count and time.monotonic() < deadline:
        time.sleep(0.05)
    return list(_grandchild_pool._processes)


@linux_only
@pytest.mark.parametrize(("kill_group", "grandchildren_survive"), [(True, False), (False, True)])
def test_killing_the_group_reaches_what_the_child_started(kill_group, grandchildren_survive):
    """Killing the child alone leaves its own children running."""
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("fork"),
        initializer=tie_to_parent,
        initargs=(os.getpid(),),
    )
    grandchildren = pool.submit(_start_grandchildren, 2).result(timeout=120)
    child = next(iter(pool._processes))
    try:
        if kill_group:
            kill_process_group(child)
        else:
            os.kill(child, signal.SIGKILL)
        assert not _alive_after(child, 2)
        assert [_alive_after(pid, 2) for pid in grandchildren] == [grandchildren_survive] * 2
    finally:
        for pid in grandchildren:
            _kill_quietly(pid)
        pool.shutdown(wait=False, cancel_futures=True)


@linux_only
@pytest.mark.parametrize(("parent_pid", "exitcode"), [(None, 0), (-1, 1)])
def test_a_child_whose_parent_is_already_gone_exits(parent_pid, exitcode):
    """A parent that died before the signal was set would never deliver it."""
    ctx = multiprocessing.get_context("fork")
    child = ctx.Process(target=tie_to_parent, args=(os.getpid() if parent_pid is None else parent_pid,))
    child.start()
    child.join(timeout=30)

    assert child.exitcode == exitcode


def test_a_child_that_leads_no_group_is_killed_alone():
    """Killed before its initializer ran, a child is still in its parent's group."""
    child = multiprocessing.get_context("fork").Process(target=time.sleep, args=(60,))
    child.start()

    kill_process_group(child.pid)
    child.join(timeout=10)

    assert child.exitcode == -signal.SIGKILL


def test_killing_a_process_that_is_already_gone_does_not_raise():
    child = multiprocessing.get_context("fork").Process(target=time.sleep, args=(0,))
    child.start()
    child.join(timeout=10)

    kill_process_group(child.pid)
