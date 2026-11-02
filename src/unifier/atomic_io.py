"""Durable replacement for state files in an already prepared directory.

Callers own locking, directory permissions and serialization. A failure before
replace leaves the old file intact. A directory fsync failure is propagated
after publication: the new bytes may be visible but durability is unconfirmed.
This is not a multi-file transaction or a no-overwrite publication primitive.
"""

import os
from pathlib import Path
import tempfile


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace(path: Path, payload: bytes, *, prefix: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
