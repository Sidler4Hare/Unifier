"""Routing policy for native package files.

A file format selects candidate targets from the catalog. The native package
manager of the chosen target still checks metadata, signature, dependency
and architecture. One format can match more than one target; rpm does not
decide between Fedora and a later rpm family by itself.
"""
import hashlib
from pathlib import Path

from multilinux.catalog import describe
from multilinux.errors import MultilinuxError


PREPARED_FORMATS = {
    "deb": ("debian13",),
    "rpm": ("fedora44",),
    "arch": ("arch",),
}


def route_package(raw, targets=None):
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise MultilinuxError(f"local package is not a file: {path}")
    with path.open("rb") as source:
        magic = source.read(8)
        source.seek(0)
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    format_name = _format_name(path.name, magic)
    candidates = _candidates(format_name, targets)
    if not candidates:
        raise MultilinuxError(f"no target accepts {format_name} packages")
    return {
        "path": str(path),
        "sha256": digest,
        "format": format_name,
        "candidates": candidates,
        "target": candidates[0] if len(candidates) == 1 else None,
        "size_bytes": path.stat().st_size,
        "validation": "native-package-manager-required",
    }


def _format_name(name: str, magic: bytes) -> str:
    if name.endswith(".deb") and magic == b"!<arch>\n":
        return "deb"
    if name.endswith(".rpm") and magic.startswith(b"\xed\xab\xee\xdb"):
        return "rpm"
    arch_magic = (
        (name.endswith(".pkg.tar.zst") and magic.startswith(b"\x28\xb5\x2f\xfd"))
        or (name.endswith(".pkg.tar.xz") and magic.startswith(b"\xfd7zXZ\x00"))
        or (name.endswith(".pkg.tar.gz") and magic.startswith(b"\x1f\x8b"))
    )
    if arch_magic:
        return "arch"
    raise MultilinuxError(
        "unsupported package format or extension/header mismatch; "
        "expected .deb, .rpm or .pkg.tar.*")


def _candidates(format_name: str, targets) -> list[str]:
    if targets is None:
        return list(PREPARED_FORMATS[format_name])
    found = []
    for name, raw in targets.items():
        if describe(name, raw)["package_format"] == format_name:
            found.append(name)
    found.sort()
    return found
