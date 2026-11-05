"""Bounded cross-process coordination within one physical management root.

This is a non-reentrant admission lock, taken before an environment's package
or lifecycle locks. The inode is retained even after a crash: unlinking an
advisory lock would let different waiters lock different inodes.
"""

from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import time

from unifier.errors import UnifierError


@contextmanager
def management_lock(root: Path, *, timeout: float = 30.0, cancel=None,
                    blocking: bool = True):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout < 0):
        raise UnifierError("package coordination timeout must be finite and nonnegative")
    if cancel is not None and (not callable(getattr(cancel, "is_set", None))
                               or not callable(getattr(cancel, "wait", None))):
        raise UnifierError("package coordination cancellation must be an Event")
    path = Path(root).resolve() / ".lab/state/operations/management.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise UnifierError("PACKAGE_COORDINATION_CANCELLED: waiting for management root")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not blocking:
                    raise UnifierError("PACKAGE_COORDINATION_BUSY: management root is busy") from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UnifierError("PACKAGE_COORDINATION_TIMEOUT: waiting for management root") from exc
                if cancel is not None:
                    cancel.wait(min(0.05, remaining))
                else:
                    time.sleep(min(0.05, remaining))
        # Cancellation racing successful acquisition must still precede writes.
        if cancel is not None and cancel.is_set():
            raise UnifierError("PACKAGE_COORDINATION_CANCELLED: waiting for management root")
        yield
    finally:
        os.close(descriptor)
