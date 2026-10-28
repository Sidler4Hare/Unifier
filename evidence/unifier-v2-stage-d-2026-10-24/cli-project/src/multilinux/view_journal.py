"""Small durable journal for one view's Podman storage reference.

The broker writes mounting before `podman mount`, mounted after it succeeds,
releasing before `podman umount`, and released after success. Recovery may only
release a reference whose journal says `mounted`; `mounting`/`releasing`/
`release-failed` are unresolved windows and must stay blocked.
"""

import json
import os
from pathlib import Path
import tempfile


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_journal(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".storage-journal-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        fsync_directory(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_journal(path: str | None):
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
