"""Guest-semantics path resolution and read-only directory listing.

A view mount contains the guest filesystem, but joining ``Path(mount) / guest``
and calling ``resolve()`` would follow an absolute guest symlink (for example
``/etc``) into the *host* root.  Resolution here is anchored at the view root:
every component is opened with ``O_NOFOLLOW`` relative to a directory fd, and
an absolute symlink target restarts from the same view root.  ``..`` is clamped
at that root.  The host namespace is never consulted.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path

from unifier.errors import UnifierError


MAX_SYMLINKS = 40
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_SYMLINK_FLAGS = _DIRECTORY_FLAGS | getattr(os, "O_NOFOLLOW", 0)


def _split(path: str) -> list[str]:
    return [part for part in path.split("/") if part not in {"", "."}]


def _reset_to_root(root_fd: int, stack: list[tuple[str, int]]) -> None:
    for _name, descriptor in stack:
        try:
            os.close(descriptor)
        except OSError:
            pass
    stack.clear()
    stack.append(("", os.dup(root_fd)))


def _resolve(root_fd: int, start_fd: int, raw: str, *,
             projected_root: str | None = None) -> list[tuple[str, int]]:
    if not isinstance(raw, str) or "\x00" in raw:
        raise UnifierError("guest path contains an invalid character")
    if not raw.startswith("/"):
        raise UnifierError(f"guest path must be absolute: {raw}")
    stack: list[tuple[str, int]] = [("", os.dup(start_fd))]
    pending = _split(raw)
    symlinks = 0
    try:
        while pending:
            part = pending.pop(0)
            if part == "..":
                if len(stack) > 1:
                    _name, descriptor = stack.pop()
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                continue
            current = stack[-1][1]
            try:
                descriptor = os.open(part, _SYMLINK_FLAGS, dir_fd=current)
            except OSError as exc:
                if exc.errno not in {errno.ELOOP, errno.ENOTDIR}:
                    raise
                try:
                    target = os.readlink(part, dir_fd=current)
                except OSError as read_exc:
                    if read_exc.errno in {errno.EINVAL, errno.ENOTDIR}:
                        raise OSError(errno.ENOTDIR,
                                      "guest path component is not a directory", part) from read_exc
                    raise
                if not target:
                    raise UnifierError(f"guest symlink has an empty target: {part}")
                symlinks += 1
                if symlinks > MAX_SYMLINKS:
                    raise OSError(errno.ELOOP, "too many guest symlink levels", raw)
                if target.startswith("/"):
                    if projected_root is not None:
                        # FUSE READLINK has already converted the guest link
                        # to a host-visible path under this exact mount.  Strip
                        # that prefix once, then resolve under the anchored FD.
                        # A raw guest absolute link uses the other mode.
                        if target == projected_root:
                            target = "/"
                        elif target.startswith(projected_root.rstrip("/") + "/"):
                            target = target[len(projected_root.rstrip("/")):]
                        else:
                            raise UnifierError(
                                "projected symlink target is outside its view mount")
                    _reset_to_root(root_fd, stack)
                pending = _split(target) + pending
                continue
            stack.append((part, descriptor))
        return stack
    except BaseException:
        for _name, descriptor in stack:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


@contextmanager
def open_guest_directory(root: Path, guest_path: str = "/", *, projected: bool = False):
    """Yield ``(fd, canonical_guest_path)`` anchored below a view root."""
    root = Path(root).absolute()
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise UnifierError(f"view root is unavailable: {root}: {exc}") from exc
    stack: list[tuple[str, int]] = []
    try:
        stack = _resolve(root_fd, root_fd, guest_path,
                         projected_root=str(root) if projected else None)
        canonical = "/" + "/".join(name for name, _fd in stack[1:] if name)
        if canonical == "/" + "":
            canonical = "/"
        yield stack[-1][1], canonical
    finally:
        for _name, descriptor in stack:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(root_fd)
        except OSError:
            pass


def _entry_is_directory(root_fd: int, canonical_parent: str, name: str, *,
                        projected_root: str | None = None) -> bool:
    stack: list[tuple[str, int]] = []
    try:
        # Reconstruct the complete guest ancestry, rather than pretending that
        # the parent FD is the root.  ../ in a relative link must be able to
        # reach that parent's real guest ancestor.
        path = canonical_parent.rstrip("/") + "/" + name
        stack = _resolve(root_fd, root_fd, path, projected_root=projected_root)
        stat = os.fstat(stack[-1][1])
        return (stat.st_mode & 0o170000) == 0o040000
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            return False
        raise
    finally:
        for _name, descriptor in stack:
            try:
                os.close(descriptor)
            except OSError:
                pass


def list_guest_directories(root: Path, guest_path: str = "/", *,
                           projected: bool = False) -> dict:
    """Return real immediate subdirectories with guest path semantics.

    Symlinks are included only when they resolve to a directory *inside the
    same view root*.  A broken link or a non-directory is omitted; the target
    directory's own errors are reported, not converted into an empty listing.
    """
    root = Path(root).absolute()
    with open_guest_directory(root, guest_path, projected=projected) as (fd, canonical):
        names = []
        try:
            entries = list(os.scandir(fd))
        except OSError as exc:
            raise UnifierError(f"cannot list guest directory {canonical}: {exc}") from exc
        try:
            resolved_root_fd = os.open(root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise UnifierError(f"view root is unavailable: {root}: {exc}") from exc
        try:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                    elif entry.is_symlink() and _entry_is_directory(
                            resolved_root_fd, canonical, entry.name,
                            projected_root=str(root) if projected else None):
                        names.append(entry.name)
                except OSError as exc:
                    if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                        continue
                    raise UnifierError(
                        f"cannot inspect guest entry {canonical}/{entry.name}: {exc}") from exc
        finally:
            try:
                os.close(resolved_root_fd)
            except OSError:
                pass
        return {"path": canonical, "entries": sorted(names)}


def parse_nul_listing(payload: str) -> dict:
    """Parse the fixed guest listing protocol (NUL-separated)."""
    items = [item for item in payload.split("\x00") if item != ""]
    if not items:
        raise UnifierError("guest listing returned no canonical path")
    canonical = items[0]
    names = sorted(item for item in items[1:] if "/" not in item and item not in {".", ".."})
    return {"path": canonical, "entries": names}
