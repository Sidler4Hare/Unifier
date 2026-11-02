"""Pure parsing of package paths and desktop launch candidates.

No runtime or environment service is imported here. Candidates describe a
literal argv; the service still resolves guest commands and validates every
launch binding before execution.
"""

import re
import shlex

from unifier.errors import UnifierError


_EXECUTABLE_DIRS = (
    "/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/",
    "/usr/local/bin/", "/usr/local/sbin/",
)
_APPLICATION_DIRS = ("/usr/share/applications/", "/usr/local/share/applications/")


_DESKTOP_DROP_FIELD_CODES = {
    "%f", "%F", "%u", "%U", "%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m",
}
_DESKTOP_UNSUPPORTED_FIRST = {
    "sh", "bash", "dash", "zsh", "env", "/bin/sh", "/usr/bin/sh",
    "/bin/bash", "/usr/bin/bash", "/usr/bin/env", "/bin/env",
}


def parse_desktop_exec(value: str) -> dict:
    """Parse one desktop Exec value into a literal argv.

    Field codes that name a file/URL/icon are removed; ``%%`` becomes one
    literal percent.  A shell wrapper or a first token that is not a plain
    command is reported as unsupported so the caller can demand --exec rather
    than suggest a wrong launch.  No process is started here.
    """
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError as exc:
        raise UnifierError(f"desktop Exec cannot be parsed: {exc}") from exc
    argv = []
    for token in tokens:
        if token in _DESKTOP_DROP_FIELD_CODES:
            continue
        if token == "%%":
            token = "%"
        elif token.startswith("%"):
            raise UnifierError(f"unsupported desktop Exec field code: {token}")
        else:
            token = token.replace("%%", "%")
        if re.search(r"%[A-Za-z]", token):
            raise UnifierError(f"unsupported desktop Exec field code in {token!r}")
        if not token:
            continue
        argv.append(token)
    if not argv:
        raise UnifierError("desktop Exec has no launchable argv")
    first = argv[0]
    if first in _DESKTOP_UNSUPPORTED_FIRST or "=" in first:
        raise UnifierError(
            "desktop Exec uses a shell/wrapper form; pass --exec explicitly")
    if not first.startswith("/") and not re.fullmatch(r"[A-Za-z0-9._+-]+", first):
        raise UnifierError(
            "desktop Exec first component is not a plain command or absolute path")
    return {"argv": argv, "executable": first}


def package_entry_kind(path: str) -> str | None:
    if any(path.startswith(prefix) and path.count("/") == prefix.count("/")
           for prefix in _EXECUTABLE_DIRS):
        return "executable"
    if path.endswith(".desktop") and path.startswith(_APPLICATION_DIRS):
        return "desktop"
    return None


def desktop_exec_argv(text: str) -> list[str] | None:
    """Read the first Exec line; unsupported entries require explicit --exec."""
    for line in text.splitlines():
        if line.startswith("Exec="):
            try:
                return parse_desktop_exec(line[5:].strip())["argv"]
            except UnifierError:
                return None
    return None
