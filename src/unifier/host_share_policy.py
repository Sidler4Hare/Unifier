"""Pure path decisions for automatic host sharing in one guest environment.

The trusted host adapter must verify the real path, file type, mount boundary,
Unix permissions and environment identity before using a policy decision.
This module performs no I/O and grants no access by itself.
"""


from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from unifier.errors import UnifierError


_INVALID_PARTS = ("", ".", "..")


def _components(path: str, label: str) -> tuple[str, ...]:
    """Split one already canonical absolute POSIX path into its components.

    Raises :class:`UnifierError` for a relative path, a non-canonical form
    (repeated separators, ``.``, ``..``, a trailing slash below the root), a
    NUL byte, an empty string or a non-string value.
    """
    if not isinstance(path, str):
        raise UnifierError(f"{label} must be a str, not {type(path).__name__}")
    if not path:
        raise UnifierError(f"{label} is empty")
    if "\x00" in path:
        raise UnifierError(f"{label} contains NUL: {path!r}")
    if not path.startswith("/"):
        raise UnifierError(f"{label} must be absolute: {path!r}")
    if path == "/":
        return ()
    if path.endswith("/"):
        raise UnifierError(f"{label} must not end with a slash: {path!r}")
    parts = tuple(path.split("/")[1:])
    if any(part in _INVALID_PARTS for part in parts):
        raise UnifierError(f"{label} is not canonical POSIX form: {path!r}")
    return parts


def _entries(values: Iterable[str], label: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Copy one configured path list and precompute its component tuples."""
    if isinstance(values, (str, bytes, bytearray)):
        raise UnifierError(f"{label} must be a sequence of paths, not {type(values).__name__}")
    try:
        items = tuple(values)
    except TypeError:
        raise UnifierError(
            f"{label} must be a sequence of paths, not {type(values).__name__}") from None
    parts = tuple(_components(item, f"{label} entry") for item in items)
    return items, parts


def _covers(broad: tuple[str, ...], narrow: tuple[str, ...]) -> bool:
    """Report whether ``broad`` is ``narrow`` itself or one of its ancestors."""
    return len(broad) <= len(narrow) and narrow[:len(broad)] == broad


def _within_any(candidate: tuple[str, ...], entries: tuple[tuple[str, ...], ...]) -> bool:
    return any(_covers(entry, candidate) for entry in entries)


@dataclass(frozen=True)
class HostSharePolicy:
    """Decide one guest environment's automatic sharing by path components.

    ``roots`` is the browse scope of the guest; an empty scope
    authorizes nothing.  ``blacklist`` entries and their descendants are always
    refused.  ``admin_roots`` are host-marked administrative directories: a
    path inside them is allowed only when every admin root covering that path
    appears in ``opened_admin_roots``, so nested admin roots keep their extra
    restriction.  ``writable`` says whether the guest may write.

    Scope, blacklist and admin rules are conjunctive: opening an admin
    directory never widens the scope and never defeats a blacklist hit, and a
    read-write mode never grants access to a refused path.  An opened entry
    must itself be a declared admin root, otherwise construction fails.

    ``allows`` fails closed: an invalid request path returns ``False`` instead
    of raising, because a path the adapter could not describe is not
    authorized.  The ``write`` mode must be a real bool, which is a caller
    programming error and raises ``TypeError``.
    """

    roots: tuple[str, ...] = ()
    blacklist: tuple[str, ...] = ()
    admin_roots: tuple[str, ...] = ()
    opened_admin_roots: tuple[str, ...] = ()
    writable: bool = False

    _root_parts: tuple[tuple[str, ...], ...] = field(init=False, repr=False, default=())
    _blacklist_parts: tuple[tuple[str, ...], ...] = field(init=False, repr=False, default=())
    _admin_parts: tuple[tuple[str, ...], ...] = field(init=False, repr=False, default=())
    _opened_parts: frozenset[tuple[str, ...]] = field(init=False, repr=False, default=frozenset())

    def __post_init__(self) -> None:
        if not isinstance(self.writable, bool):
            raise TypeError(
                f"host share write mode must be a bool, not {type(self.writable).__name__}")
        for name, label, private in (
            ("roots", "host share root", "_root_parts"),
            ("blacklist", "host share blacklist entry", "_blacklist_parts"),
            ("admin_roots", "host share admin root", "_admin_parts"),
        ):
            texts, parts = _entries(getattr(self, name), label)
            object.__setattr__(self, name, texts)
            object.__setattr__(self, private, parts)
        opened, opened_parts = _entries(self.opened_admin_roots, "opened host admin root")
        declared = set(self.admin_roots)
        undeclared = [text for text in opened if text not in declared]
        if undeclared:
            raise UnifierError(
                "opened admin roots are not declared admin roots: "
                + ", ".join(repr(text) for text in undeclared))
        object.__setattr__(self, "opened_admin_roots", opened)
        object.__setattr__(self, "_opened_parts", frozenset(opened_parts))

    def allows(self, verified_path: str, *, write: bool = False) -> bool:
        """Return whether this object may see one already verified host path.

        ``write=True`` asks for read-write access, which needs a policy built
        with ``writable=True``.  Non-canonical or non-absolute input returns
        ``False``; the caller keeps its own verdict for that path.
        """
        if not isinstance(write, bool):
            raise TypeError(f"host share write mode must be a bool, not {type(write).__name__}")
        try:
            candidate = _components(verified_path, "verified host path")
        except UnifierError:
            return False
        if write and not self.writable:
            return False
        if _within_any(candidate, self._blacklist_parts):
            return False
        if not _within_any(candidate, self._root_parts):
            return False
        return all(entry in self._opened_parts for entry in self._admin_parts
                   if _covers(entry, candidate))
