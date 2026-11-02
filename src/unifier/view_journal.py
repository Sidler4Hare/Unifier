"""Small durable journal for one view's Podman storage reference.

The broker writes mounting before `podman mount`, mounted after it succeeds,
releasing before `podman umount`, and released after success. Recovery may only
release a reference whose journal says `mounted`; `mounting`/`releasing`/
`release-failed` are unresolved windows and must stay blocked.
"""

import json
from pathlib import Path

from unifier.atomic_io import atomic_replace, fsync_directory


def write_journal(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    atomic_replace(target, data, prefix=".storage-journal-")


def read_journal(path: str | None):
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
