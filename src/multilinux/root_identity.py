"""Persistent Btrfs root identity, independent of its current st_dev number.

The ioctls use the opened directory, not a device path or a mount-table guess.
Other filesystems retain the existing strict device/inode registration.
"""

import errno
import fcntl
import os
import struct
import uuid

from multilinux.errors import MultilinuxError


# Linux UAPI <linux/btrfs.h>, using the asm-generic ioctl encoding supported
# by our x86-64 and AArch64 hosts. Both are read-only, zero-initialized queries.
# sizeof(fs_info_args) = 1024; fsid offset = 16.
# sizeof(get_subvol_info_args) = 504; treeid offset = 0; uuid offset = 296.
FS_INFO = 0x8400941F
GET_SUBVOL_INFO = 0x81F8943C
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def validate_identity(identity: dict) -> dict:
    if not isinstance(identity, dict) or identity.get("kind") != "btrfs-v1":
        raise MultilinuxError("data root persistent identity must be btrfs-v1")
    for key in ("filesystem_uuid", "subvolume_uuid"):
        value = identity.get(key)
        try:
            parsed = uuid.UUID(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != value:
            raise MultilinuxError(f"data root identity {key} must be a canonical UUID")
        if key == "filesystem_uuid" and parsed.int == 0:
            raise MultilinuxError("data root filesystem UUID must not be zero")
    subvolume = identity.get("subvolume_id")
    if isinstance(subvolume, bool) or not isinstance(subvolume, int) or subvolume <= 0:
        raise MultilinuxError("data root identity subvolume_id must be a positive integer")
    return identity


def _btrfs_identity(descriptor: int) -> dict | None:
    filesystem = bytearray(1024)
    try:
        fcntl.ioctl(descriptor, FS_INFO, filesystem, True)
    except OSError as exc:
        if exc.errno == errno.ENOTTY:
            return None  # Not Btrfs: keep device/inode checks, without a guess.
        raise
    subvolume = bytearray(504)
    # Once FS_INFO succeeds, failure to identify the subvolume must not downgrade
    # an existing Btrfs registration to device/inode-only checks.
    fcntl.ioctl(descriptor, GET_SUBVOL_INFO, subvolume, True)
    return validate_identity({
        "kind": "btrfs-v1",
        "filesystem_uuid": str(uuid.UUID(bytes=bytes(filesystem[16:32]))),
        "subvolume_id": struct.unpack_from("=Q", subvolume)[0],
        "subvolume_uuid": str(uuid.UUID(bytes=bytes(subvolume[296:312]))),
    })


def observe_root(locator: str) -> tuple[os.stat_result, dict | None]:
    """Read identity from one FD and reject a path changed during observation."""
    if os.path.realpath(locator) != locator:
        raise MultilinuxError("data root path was replaced by a symbolic link")
    try:
        descriptor = os.open(locator, _DIRECTORY_FLAGS)
        try:
            observed = os.fstat(descriptor)
            identity = _btrfs_identity(descriptor)
            current = os.stat(locator, follow_symlinks=False)
            if ((current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino)
                    or os.path.realpath(locator) != locator):
                raise MultilinuxError("data root path was replaced while checking its identity")
            return observed, identity
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MultilinuxError(f"data root identity is unavailable or replaced: {exc}") from exc


def resolve_identity(record: dict) -> dict:
    """Return the current device number only after verifying persistent pins.

    The stored record is never changed by a read. Frozen sessions and file
    journals still carry their original device observations and are not replayed
    across a device change by this registration-level repair.
    """
    if "root_identity" not in record:
        return dict(record)
    expected = validate_identity(record["root_identity"])
    observed, identity = observe_root(record["locator"])
    if observed.st_ino != record["root_ino"] or identity != expected:
        raise MultilinuxError("data root path was replaced (filesystem/subvolume/inode changed)")
    return {**record, "root_dev": observed.st_dev}
