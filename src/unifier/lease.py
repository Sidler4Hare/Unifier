"""Small Linux primitives for a bounded controlled move.

Nothing here is a cross-filesystem atomic rename and nothing here claims a
kernel compare-and-swap.  The two primitives are deliberately narrow:

* ``WriteLease`` takes an exclusive ``F_SETLEASE``/``F_WRLCK`` lease on one
  already-open regular file.  A write lease makes new opens/truncates wait or
  fail, and fails while another open file description or a writable mapping
  exists.  It does *not* stop rename/unlink; the controlled move therefore
  captures the source *name* into a private same-filesystem directory before it
  treats the bytes as isolated, and never unlinks an unverified source name.
* ``rename_noreplace`` exposes ``renameat2(RENAME_NOREPLACE)`` so a capture or
  restore can never silently overwrite an existing name.

Lease break notification uses one dedicated real-time signal directed at the
calling thread with ``F_SETOWN_EX``/``F_OWNER_TID``.  The signal is blocked in
that thread for the lifetime of the lease and drained with ``sigtimedwait``;
SIGIO is not ignored globally and no process-wide handler is installed.  The
controlled cleanup refuses to run off the main thread because this module will
not alter other threads' signal masks.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import signal
import struct
import threading

RENAME_NOREPLACE = 1
_F_OWNER_TID = 0
# Linux UAPI asm-generic/fcntl.h. CPython exposed this existing Linux
# operation only in 3.13; supported 3.11/3.12 interpreters still need it.
_F_SETOWN_EX = getattr(fcntl, "F_SETOWN_EX", 15)
_F_WRLCK = getattr(fcntl, "F_WRLCK", 1)
_F_UNLCK = getattr(fcntl, "F_UNLCK", 2)
# A dedicated RT signal keeps unrelated SIGIO users out of this path.
LEASE_SIGNAL = signal.SIGRTMIN + 6


class LeaseError(Exception):
    pass


class LeaseUnsupported(LeaseError):
    pass


class LeaseBusy(LeaseError):
    pass


class LeaseBroken(LeaseError):
    pass


_libc = ctypes.CDLL(None, use_errno=True)
try:
    _renameat2 = _libc.renameat2
    _renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    )
    _renameat2.restype = ctypes.c_int
except AttributeError:  # pragma: no cover - glibc on supported Linux hosts
    _renameat2 = None


def rename_noreplace(old_dir_fd: int, old_name: str, new_dir_fd: int,
                     new_name: str) -> None:
    """Atomic rename that refuses to replace an existing destination."""
    if _renameat2 is None:
        raise OSError(
            errno.ENOSYS,
            "renameat2(RENAME_NOREPLACE) is unavailable; refusing a capture "
            "that could overwrite a name")
    if not isinstance(old_name, str) or not isinstance(new_name, str):
        raise TypeError("rename names must be strings")
    result = _renameat2(
        old_dir_fd, os.fsencode(old_name), new_dir_fd, os.fsencode(new_name),
        RENAME_NOREPLACE)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _getlease(descriptor: int) -> int:
    return fcntl.fcntl(descriptor, fcntl.F_GETLEASE)


def _setlease(descriptor: int, kind: int) -> None:
    fcntl.fcntl(descriptor, fcntl.F_SETLEASE, kind)


def _set_owner_tid(descriptor: int) -> None:
    # struct f_owner_ex { int type; pid_t pid; }; main thread tid == pid.
    fcntl.fcntl(
        descriptor, _F_SETOWN_EX,
        struct.pack("ii", _F_OWNER_TID, os.getpid()))


class WriteLease:
    """Exclusive write lease with an explicit, restorable signal setup."""

    def __init__(self, descriptor: int, label: str = "file"):
        self.descriptor = descriptor
        self.label = label
        self.acquired = False
        self._old_mask = None
        self._break_seen = False

    def _drain(self) -> bool:
        drained = False
        while True:
            try:
                info = signal.sigtimedwait({LEASE_SIGNAL}, 0)
            except InterruptedError:
                continue
            if info is None:
                break
            drained = True
            self._break_seen = True
        return drained

    def acquire(self) -> None:
        if self.acquired:
            return
        if threading.current_thread() is not threading.main_thread():
            raise LeaseUnsupported(
                "the controlled source lease is only taken on the main thread; "
                "an off-main-thread request conservatively retains the source")
        try:
            self._old_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, {LEASE_SIGNAL})
        except (OSError, ValueError) as exc:
            raise LeaseUnsupported(
                f"cannot block the lease notification signal: {exc}") from exc
        try:
            fcntl.fcntl(self.descriptor, fcntl.F_SETSIG, LEASE_SIGNAL)
            _set_owner_tid(self.descriptor)
            _setlease(self.descriptor, _F_WRLCK)
        except OSError as exc:
            self._restore_mask()
            if exc.errno in {errno.EAGAIN, errno.EACCES, errno.EBUSY}:
                raise LeaseBusy(
                    f"{self.label} is open by another file description or has a "
                    "conflicting mapping; refusing controlled cleanup") from exc
            if exc.errno in {
                    errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP,
                    errno.EPERM}:
                raise LeaseUnsupported(
                    f"exclusive write leases are unsupported for {self.label}: "
                    f"{exc.strerror}") from exc
            raise LeaseError(
                f"cannot acquire an exclusive write lease on {self.label}: "
                f"{exc.strerror}") from exc
        self.acquired = True
        self._drain()

    def _restore_mask(self) -> None:
        if self._old_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, self._old_mask)
            except (OSError, ValueError):
                pass
            self._old_mask = None

    def observe(self) -> str:
        """Return 'held' or 'broken' from this description's own lease state.

        Multiple leases may share the one notification signal (source and
        target).  The kernel changes *this* file's F_GETLEASE state immediately
        when a break is requested, so pending-signal presence alone cannot be
        attributed to a particular lease and is not used here.  Draining on a
        confirmed break does not lose the other lease's evidence because its
        own F_GETLEASE check remains authoritative.
        """
        if not self.acquired:
            return "not-held"
        try:
            state = _getlease(self.descriptor)
        except OSError:
            return "broken"
        if state != _F_WRLCK:
            self._break_seen = True
            self._drain()
            return "broken"
        return "held"

    def assert_held(self) -> None:
        if self.observe() != "held":
            raise LeaseBroken(
                f"the exclusive write lease on {self.label} was broken; "
                "refusing to delete or move bytes")

    def release(self) -> None:
        if self.acquired:
            try:
                _setlease(self.descriptor, _F_UNLCK)
            except OSError:
                pass
            # Any break signal generated before the release is already queued;
            # drain after releasing the lease so restoring the mask cannot
            # deliver a default-action real-time signal.
            self._drain()
            self.acquired = False
        self._restore_mask()
