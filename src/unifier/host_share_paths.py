"""One-use trusted-host path check for automatic host sharing.

:mod:`unifier.host_share_policy` decides by path text only.  This adapter turns
one authorized path text into a verified descriptor: it resolves the path with
the real host user identity, refuses every symlink (the ancestors of a root,
the target itself and each intermediate link), never crosses a mount boundary
and never falls back to a weaker resolution.  It reads no file content, creates
nothing, is not FUSE integration and does not grant final file access by
itself.  It proves only that the object behind the requested path was present,
of an acceptable kind and usable by the effective host identity at this instant;
that proof belongs to the yielded descriptor, so a caller must not reopen the
original string later and assume the same object.
"""

from __future__ import annotations

import ctypes
import os
import platform
import stat
import struct
import sys
from contextlib import contextmanager
from typing import Iterator

from unifier.errors import UnifierError
from unifier.host_share_policy import HostSharePolicy

__all__ = ["open_host_share_path"]

# Linux UAPI for the supported x86_64 and aarch64 ABIs.
_SYS_openat2 = 437
_AT_FDCWD = -100
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_FLAGS_CHILD = os.O_PATH | os.O_CLOEXEC
_FLAGS_ROOT = _FLAGS_CHILD | os.O_DIRECTORY
_RESOLVE_CHILD = _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_XDEV


class _OpenHow(ctypes.Structure):
    _fields_ = (("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                ("resolve", ctypes.c_uint64))


_libc: ctypes.CDLL | None = None


def _supported() -> bool:
    """Report whether the process ABI is supported by this wrapper."""
    return (sys.platform == "linux" and struct.calcsize("P") == 8
            and platform.machine() in ("x86_64", "aarch64"))


def _openat2(dir_fd: int, path: str, flags: int, resolve: int) -> int:
    """Open one path with ``openat2`` and these resolve flags, or refuse."""
    global _libc
    library = _libc
    if library is None:
        library = ctypes.CDLL(None, use_errno=True)
        library.syscall.restype = ctypes.c_long
        library.syscall.argtypes = [ctypes.c_long]
        _libc = library
    how = _OpenHow(flags, 0, resolve)
    raw = library.syscall(_SYS_openat2, ctypes.c_int(dir_fd),
                          ctypes.c_char_p(os.fsencode(path)), ctypes.byref(how),
                          ctypes.c_size_t(ctypes.sizeof(how)))
    if raw < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
    return int(raw)


def _parts(text: str) -> tuple[str, ...]:
    """Split one policy-validated canonical absolute path into components."""
    return () if text == "/" else tuple(text.split("/")[1:])


def _longest_root(policy: HostSharePolicy, parts: tuple[str, ...]) -> tuple[str, ...]:
    """Return the most specific entry of ``policy.roots`` covering ``parts``."""
    best: tuple[str, ...] | None = None
    for entry in policy.roots:
        current = _parts(entry)
        if len(current) <= len(parts) and parts[:len(current)] == current:
            if best is None or len(current) > len(best):
                best = current
    if best is None:
        raise UnifierError("no configured host share root covers the path")
    return best


def _check_link(fd: int, expected: str) -> None:
    """Refuse a descriptor whose object no longer names ``expected``."""
    try:
        names = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as exc:
        raise UnifierError(f"cannot resolve descriptor for {expected!r}: {exc}") from None
    if names != expected:
        raise UnifierError(f"descriptor names {names!r}, not the requested {expected!r}")


def _check_target(fd: int, write: bool) -> None:
    """Accept a directory, or a single-link regular file, usable by this host user."""
    info = os.fstat(fd)
    is_dir = stat.S_ISDIR(info.st_mode)
    if not is_dir and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
        raise UnifierError(f"unsupported host object: kind 0o{stat.S_IFMT(info.st_mode):o} "
                           f"with {info.st_nlink} link(s)")
    mode = (os.R_OK | os.X_OK) if is_dir else os.R_OK
    if write:
        mode |= os.W_OK
    link = f"/proc/self/fd/{fd}"
    if not os.access(link, mode, effective_ids=True):
        raise UnifierError(f"host identity lacks mode 0o{mode:o} on {link}")


@contextmanager
def open_host_share_path(policy: HostSharePolicy, path: str, *,
                         write: bool = False) -> Iterator[int]:
    """Yield one borrowed ``O_PATH`` descriptor for one authorized host ``path``.

    The policy verdict comes first and costs no syscall; a refusal raises
    :class:`UnifierError`, while a non-bool ``write`` keeps raising ``TypeError``
    exactly as the policy does. The adapter must supply only trusted local
    roots and run under the host user's effective identity. The absolute root
    is opened without symlinks and the rest of the path from its descriptor
    without escaping it, crossing a mount or following a symlink.  The caller
    must not close the yielded descriptor: this generator closes it, and every
    descriptor it opened, on normal exit and on error alike.
    """
    if not policy.allows(path, write=write):
        raise UnifierError(f"host share policy refuses {path!r} with write={write}")
    if not _supported():
        raise UnifierError("openat2 RESOLVE_* is unavailable outside "
                           "Linux 64-bit x86_64 or aarch64")
    parts = _parts(path)
    root_parts = _longest_root(policy, parts)
    root = "/" + "/".join(root_parts)
    root_fd = -1
    child_fd = -1
    try:
        try:
            root_fd = _openat2(_AT_FDCWD, root, _FLAGS_ROOT, _RESOLVE_NO_SYMLINKS)
            below = "/".join(parts[len(root_parts):])
            if below:
                child_fd = _openat2(root_fd, below, _FLAGS_CHILD, _RESOLVE_CHILD)
            fd = root_fd if child_fd < 0 else child_fd
            _check_target(fd, write)
            _check_link(root_fd, root)
            if child_fd >= 0:
                _check_link(child_fd, path)
        except (OSError, UnicodeError) as exc:
            raise UnifierError(f"cannot verify host share path {path!r}: {exc}") from exc
        yield fd
    finally:
        # A close error on these borrowed handles must not displace the verdict
        # already in flight, and the request-as-root case owns one descriptor.
        for owned in (child_fd, root_fd):
            if owned >= 0:
                try:
                    os.close(owned)
                except OSError:
                    pass
