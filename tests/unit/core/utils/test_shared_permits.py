"""A limit shared by processes no common parent started, kept through kills.

Run with real processes: what is under test is how the kernel treats their
locks, which no fake can show.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import threading
import time

from core.utils import shared_permits
from core.utils.shared_permits import PermitShare, SharedPermits

_fork = multiprocessing.get_context("fork")


def _holder(permits: SharedPermits) -> tuple[threading.Event, threading.Event]:
    """A thread holding one permit, once ``acquired`` is set, until ``release`` is."""
    acquired, release = threading.Event(), threading.Event()

    def hold() -> None:
        with permits.acquire():
            acquired.set()
            release.wait(30)

    threading.Thread(target=hold, daemon=True).start()
    return acquired, release


def _count_holders(directory: str, requests: int, in_flight, peak, lock) -> None:
    permits = SharedPermits(directory)

    def one() -> None:
        with permits.acquire():
            with lock:
                in_flight.value += 1
                peak.value = max(peak.value, in_flight.value)
            time.sleep(0.05)
            with lock:
                in_flight.value -= 1

    threads = [threading.Thread(target=one) for _ in range(requests)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _take_and_wait(directory: str, count: int, ready) -> None:
    permits = SharedPermits(directory)
    with contextlib.ExitStack() as held:
        for _ in range(count):
            held.enter_context(permits.acquire())
        ready.set()
        time.sleep(60)


def _share_and_wait(directory: str, count: int, ready) -> None:
    PermitShare(directory, count)
    ready.set()
    time.sleep(60)


def _killed(process) -> None:
    os.kill(process.pid, signal.SIGKILL)
    process.join(timeout=10)


def test_processes_never_hold_more_permits_than_the_shares_give(tmp_path):
    share = PermitShare(str(tmp_path), 3)
    in_flight, peak, lock = _fork.Value("i", 0), _fork.Value("i", 0), _fork.Lock()
    processes = [_fork.Process(target=_count_holders, args=(str(tmp_path), 6, in_flight, peak, lock)) for _ in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)

    assert [process.exitcode for process in processes] == [0, 0, 0]
    assert peak.value == 3
    share.close()


def test_a_killed_holder_gives_its_permits_back(tmp_path):
    PermitShare(str(tmp_path), 2)
    ready = _fork.Event()
    holder = _fork.Process(target=_take_and_wait, args=(str(tmp_path), 2, ready))
    holder.start()
    assert ready.wait(30)
    permits = SharedPermits(str(tmp_path))
    acquired, release = _holder(permits)
    assert not acquired.wait(0.3)

    _killed(holder)

    assert acquired.wait(5)
    release.set()


def test_a_dead_shares_permits_stop_counting(tmp_path):
    ready = _fork.Event()
    owner = _fork.Process(target=_share_and_wait, args=(str(tmp_path), 5, ready))
    owner.start()
    assert ready.wait(30)
    _killed(owner)
    PermitShare(str(tmp_path), 1)
    permits = SharedPermits(str(tmp_path))

    first, release_first = _holder(permits)
    second, release_second = _holder(permits)

    assert first.wait(5)
    assert not second.wait(0.3)
    assert len(os.listdir(tmp_path)) == 2  # the live share's owner and permit
    release_first.set()
    assert second.wait(5)
    release_second.set()


def test_the_shares_in_one_directory_add_up(tmp_path):
    PermitShare(str(tmp_path), 2)
    PermitShare(str(tmp_path), 3)
    permits = SharedPermits(str(tmp_path))

    holders = [_holder(permits) for _ in range(6)]

    assert sum(acquired.wait(1) for acquired, _ in holders) == 5
    for _, release in holders:
        release.set()


def test_one_thread_per_process_polls_while_the_others_wait(tmp_path, monkeypatch):
    """Every waiting thread polling on its own took nearly 11 cores at 256 threads."""
    PermitShare(str(tmp_path), 1)
    permits = SharedPermits(str(tmp_path))
    held, release_held = _holder(permits)
    assert held.wait(5)
    tries = []
    try_lock = shared_permits._try_lock
    monkeypatch.setattr(shared_permits, "_try_lock", lambda path: tries.append(path) or try_lock(path))

    waiters = [_holder(permits) for _ in range(20)]
    time.sleep(0.5)

    assert 0 < len(tries) < 20
    for _, release in waiters:
        release.set()
    release_held.set()
    assert all(acquired.wait(10) for acquired, _ in waiters)


def test_a_closed_share_is_withdrawn(tmp_path):
    share = PermitShare(str(tmp_path), 2)

    share.close()
    share.close()

    assert os.listdir(tmp_path) == []


def test_without_a_live_share_the_work_runs_unlimited(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(shared_permits, "logger", type("L", (), {"warning": lambda self, m: warnings.append(m)})())
    permits = SharedPermits(str(tmp_path / "missing"))

    for _ in range(2):
        with permits.acquire():
            pass

    assert len(warnings) == 1 and "No live permit share" in warnings[0]


def test_a_share_added_later_counts_after_the_refresh(tmp_path):
    PermitShare(str(tmp_path), 1)
    permits = SharedPermits(str(tmp_path), refresh_interval=0.1)
    first, release_first = _holder(permits)
    assert first.wait(5)
    second, release_second = _holder(permits)
    assert not second.wait(0.3)

    PermitShare(str(tmp_path), 1)

    assert second.wait(5)
    release_first.set()
    release_second.set()
