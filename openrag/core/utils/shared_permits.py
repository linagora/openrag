"""A limit on concurrent work, shared by every process of one machine.

Marker's slots each run in a child process, under actors that are themselves
separate processes, and all of them call one OCR server that takes a fixed
number of requests at once. A ``multiprocessing`` semaphore can't serve them:
it only reaches the processes its creator starts, and a process killed while
holding a permit never gives it back.

Here a permit is a file, held with an exclusive ``flock``. Any process can open
it by path, and the kernel releases the lock when its holder dies, however it
dies. Permits come in shares: :class:`PermitShare` adds some to a directory for
as long as the process that made it lives, and :class:`SharedPermits` hands out
one permit of any live share in that directory at a time.
"""

from __future__ import annotations

import fcntl
import os
import random
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from core.utils.logging import get_logger

logger = get_logger()

_OWNER = "owner"


class PermitShare:
    """``count`` permits in ``directory``, for as long as this process lives.

    Its owner file stays locked by this process, which is how other processes
    tell a live share from one whose process has died.
    """

    def __init__(self, directory: str, count: int) -> None:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.directory = directory
        self.count = count
        self._key = f"{os.getpid()}-{secrets.token_hex(4)}"
        for index in range(count):
            os.close(os.open(self._path(str(index)), os.O_CREAT | os.O_WRONLY, 0o600))
        # Locked before it gets its name, and named after the permits exist, so
        # no process sees this share without its permits, or takes it for dead.
        staging = os.path.join(directory, f".{self._key}.staging")
        self._fd: int | None = os.open(staging, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        os.rename(staging, self._path(_OWNER))

    def _path(self, suffix: str) -> str:
        return os.path.join(self.directory, f"{self._key}.{suffix}")

    def close(self) -> None:
        """Withdraw the share; a request holding one of its permits finishes."""
        if self._fd is None:
            return
        _remove_share(self.directory, self._key)
        os.close(self._fd)
        self._fd = None


class SharedPermits:
    """Takes one permit of the live shares in ``directory`` per :meth:`acquire`.

    Waiting is polling: ``flock`` can wait on one file, not on whichever of
    several frees first. Only one thread per process polls at a time, the
    others waiting their turn on a thread lock, and the pause between two
    rounds doubles from ``poll_interval`` up to ``max_poll_interval``. Measured,
    256 threads polling every 20 ms while all 32 permits were busy took nearly
    11 cores. The shares are listed again every ``refresh_interval`` seconds,
    so one added or gone counts from then on.
    """

    def __init__(
        self,
        directory: str,
        *,
        poll_interval: float = 0.01,
        max_poll_interval: float = 0.1,
        refresh_interval: float = 2.0,
    ) -> None:
        self.directory = directory
        self._poll_interval = poll_interval
        self._max_poll_interval = max_poll_interval
        self._refresh_interval = refresh_interval
        self._turn = threading.Lock()
        self._permits: list[str] = []
        self._listed_at = float("-inf")
        self._warned = False

    def _live_permits(self) -> list[str]:
        now = time.monotonic()
        if not self._permits or now - self._listed_at >= self._refresh_interval:
            self._permits = _live_permits(self.directory)
            self._listed_at = now
        return self._permits

    def _take(self) -> int | None:
        """A locked permit's descriptor, or ``None`` when there is no live share."""
        pause = self._poll_interval
        while True:
            permits = self._live_permits()
            if not permits:
                if not self._warned:
                    self._warned = True
                    logger.warning(f"No live permit share in {self.directory}: running without a limit")
                return None
            for path in random.sample(permits, len(permits)):
                fd = _try_lock(path)
                if fd is not None:
                    return fd
            time.sleep(pause)
            pause = min(pause * 2, self._max_poll_interval)

    @contextmanager
    def acquire(self) -> Iterator[None]:
        """Hold a permit for the block, waiting for one to free if none is.

        With no live share at all there is nothing to wait for, so the block
        runs without a permit rather than never.
        """
        with self._turn:
            fd = self._take()
        if fd is None:
            yield
            return
        try:
            yield
        finally:
            os.close(fd)


def _try_lock(path: str) -> int | None:
    """An open, exclusively locked descriptor of ``path``, or ``None`` if taken or gone."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _is_held(path: str) -> bool:
    """Whether a process holds ``path``'s exclusive lock: a share's owner is alive."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _remove_share(directory: str, key: str) -> None:
    """Delete a share's files, its owner last: a share whose owner is left looks dead."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return
    owner = f"{key}.{_OWNER}"
    for name in [n for n in names if n.startswith(f"{key}.") and n != owner] + [owner]:
        try:
            os.unlink(os.path.join(directory, name))
        except FileNotFoundError:
            pass


def _live_permits(directory: str) -> list[str]:
    """The permits of every live share in ``directory``; dead shares are deleted."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    permits = []
    for name in names:
        key, _, suffix = name.rpartition(".")
        if suffix != _OWNER or name.startswith("."):
            continue
        if not _is_held(os.path.join(directory, name)):
            _remove_share(directory, key)
            continue
        permits.extend(
            os.path.join(directory, other)
            for other in names
            if other.startswith(f"{key}.") and other.rpartition(".")[2].isdigit()
        )
    return permits
