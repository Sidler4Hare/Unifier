"""Native package commands for Debian, Fedora and Arch.

Dependency resolution, version comparison and the phases inside one tool stay
with apt, dnf and pacman. This module only names those commands, rejects
inputs that would change their meaning, and reads host package state for the
isolation check. It does not import the environment service.
"""

import hashlib
from pathlib import Path
import re
import subprocess

from multilinux.errors import MultilinuxError


PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
TARGETS = ("debian13", "fedora44", "arch")
INCOMING_DIR = "/tmp/ml-incoming"

# Official Arch images strip the local signing key. The first writable
# transaction initializes the keyring and keeps signature checks on.
TIMEOUT_APT_UPDATE = 180
TIMEOUT_APT_CHANGE = 300
TIMEOUT_DNF = 300
TIMEOUT_PACMAN_KEY = 180
TIMEOUT_PACMAN_SYNC = 900
TIMEOUT_PACMAN_REMOVE = 300

LOCAL_SUFFIX = {
    "debian13": (".deb",),
    "fedora44": (".rpm",),
    "arch": (".pkg.tar.zst", ".pkg.tar.xz", ".pkg.tar.gz"),
}

_INDEX_MARKERS = (
    "Unable to locate package",
    "No match for argument",
    "target not found",
)
_SIGNATURE_MARKERS = (
    "no_pubkey",
    "invalid signature",
    "bad signature",
    "signature verification",
    "pgp signature",
    "not signed",
    "gpgme error",
)
_NETWORK_MARKERS = (
    "Could not resolve",
    "Temporary failure resolving",
    "Failed to download",
)

# A package transaction can exit 0 and still leave maintenance hooks broken.
# pacman and rpm report those failures only as text on the tool's streams, so
# the complete stdout and stderr are scanned for the explicit markers below.
# Ordinary container conditions ("Skipped: Current root is not booted."),
# dependency notices and generic "error:" lines are not collected.
MAINTENANCE_LINE_LIMIT = 300
MAINTENANCE_REASONS = (
    "pacman-hook-failed",
    "rpm-scriptlet-failed",
    "modprobe-fatal",
    "systemd-not-pid1",
    "systemd-system-bus-unavailable",
    "udev-uevent-write-failed",
)
_MAINTENANCE_IGNORED = ("ignoring",)
_PACMAN_HOOK_FAILED = re.compile(r"command failed to execute correctly", re.IGNORECASE)
_RPM_SCRIPTLET_FAILED = re.compile(
    r"scriptlet fail|error in [a-z]*\s*scriptlet", re.IGNORECASE)
_MODPROBE_FATAL = re.compile(r"\bmodprobe:\s*fatal:", re.IGNORECASE)
_SYSTEMD_NOT_PID1 = re.compile(
    r"system has not been booted with systemd as init system", re.IGNORECASE)
_SYSTEMD_SYSTEM_BUS = re.compile(
    r"failed to connect to system scope bus", re.IGNORECASE)
_UDEV_UEVENT_WRITE = re.compile(r"failed to write\b.*?/uevent", re.IGNORECASE)
# reason, tool, targets, pattern
_MAINTENANCE_RULES = (
    ("pacman-hook-failed", "pacman", frozenset({"arch"}), _PACMAN_HOOK_FAILED),
    ("rpm-scriptlet-failed", "rpm", frozenset({"fedora44"}), _RPM_SCRIPTLET_FAILED),
    ("modprobe-fatal", "modprobe", frozenset(TARGETS), _MODPROBE_FATAL),
    ("systemd-not-pid1", "systemd", frozenset(TARGETS), _SYSTEMD_NOT_PID1),
    ("systemd-system-bus-unavailable", "systemd", frozenset(TARGETS), _SYSTEMD_SYSTEM_BUS),
    ("udev-uevent-write-failed", "udev", frozenset(TARGETS), _UDEV_UEVENT_WRITE),
)


def validate_package_name(name: str) -> str:
    if not isinstance(name, str) or not PACKAGE_NAME.fullmatch(name):
        raise MultilinuxError(
            "package name must start with a letter or digit and contain only "
            "letters, digits, dots, underscores, plus signs and hyphens")
    return name


def validate_use_argv(argv: list[str] | None) -> list[str]:
    if not argv:
        return []
    if any(not isinstance(item, str) or item == "" for item in argv):
        raise MultilinuxError("use command must be a list of non-empty strings")
    return list(argv)


def native_env(target: str) -> dict[str, str]:
    """English messages, still UTF-8.

    `LC_ALL=C` makes GnuPG print Latin-1 names such as "Keßler", and those
    bytes are not valid UTF-8. `C.UTF-8` is present in all three locked images.
    """
    _require_target(target)
    env = {"LC_ALL": "C.UTF-8"}
    if target == "debian13":
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["APT_LISTCHANGES_FRONTEND"] = "none"
    return env


def install_commands(target: str, packages: list[str]) -> list[tuple[list[str], int]]:
    """Repository install. Arch refreshes and upgrades the whole environment.

    `pacman -Sy <package>` is a partial upgrade and is not produced here.
    """
    _require_target(target)
    if target == "debian13":
        return [
            (["apt-get", "update"], TIMEOUT_APT_UPDATE),
            (["apt-get", "install", "-y", *packages], TIMEOUT_APT_CHANGE),
        ]
    if target == "fedora44":
        return [(["dnf", "install", "-y", *packages], TIMEOUT_DNF)]
    return [(["pacman", "-Syu", "--noconfirm", "--needed", *packages], TIMEOUT_PACMAN_SYNC)]


def update_commands(target: str) -> list[tuple[list[str], int]]:
    _require_target(target)
    if target == "debian13":
        return [
            (["apt-get", "update"], TIMEOUT_APT_UPDATE),
            (["apt-get", "upgrade", "-y"], TIMEOUT_APT_CHANGE),
        ]
    if target == "fedora44":
        return [(["dnf", "upgrade", "-y"], TIMEOUT_DNF)]
    return [(["pacman", "-Syu", "--noconfirm"], TIMEOUT_PACMAN_SYNC)]


def remove_commands(target: str, packages: list[str]) -> list[tuple[list[str], int]]:
    _require_target(target)
    if target == "debian13":
        return [(["apt-get", "remove", "-y", *packages], TIMEOUT_APT_CHANGE)]
    if target == "fedora44":
        return [(["dnf", "remove", "-y", *packages], TIMEOUT_DNF)]
    return [(["pacman", "-R", "--noconfirm", *packages], TIMEOUT_PACMAN_REMOVE)]


def local_commands(target: str, container_path: str) -> list[tuple[list[str], int]]:
    """Install one file already copied into the environment.

    Arch still runs a full `pacman -Syu` before `-U`, so the local file is
    not applied on top of a stale database.
    """
    _require_target(target)
    if target == "debian13":
        return [
            (["apt-get", "update"], TIMEOUT_APT_UPDATE),
            (["apt-get", "install", "-y", container_path], TIMEOUT_APT_CHANGE),
        ]
    if target == "fedora44":
        return [(["dnf", "install", "-y", container_path], TIMEOUT_DNF)]
    return [
        (["pacman", "-Syu", "--noconfirm"], TIMEOUT_PACMAN_SYNC),
        (["pacman", "-U", "--noconfirm", container_path], TIMEOUT_PACMAN_SYNC),
    ]


def arch_keyring_commands(*, populate: bool) -> list[tuple[list[str], int]]:
    steps = [(["pacman-key", "--init"], TIMEOUT_PACMAN_KEY)]
    if populate:
        steps.append((["pacman-key", "--populate", "archlinux"], TIMEOUT_PACMAN_KEY))
    return steps


def query_argv(target: str, name: str) -> list[str]:
    _require_target(target)
    if target == "debian13":
        return ["dpkg-query", "-W", "-f=${Package}\t${Version}\n", name]
    if target == "fedora44":
        return ["rpm", "-q", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}.%{ARCH}\n", name]
    return ["pacman", "-Q", name]


def explain_native_error(text: str) -> str | None:
    """Short cause for a non-zero native tool. The raw tail stays beside it."""
    if any(marker in text for marker in _INDEX_MARKERS):
        return "The package is not in the repository index."
    lowered = text.lower()
    if any(marker in lowered for marker in _SIGNATURE_MARKERS) or (
            "signature" in lowered and "invalid" in lowered):
        return "A signature check rejected the payload."
    if any(marker in text for marker in _NETWORK_MARKERS):
        return "The repository could not be reached."
    return None


def maintenance_findings(target: str, stdout: str, stderr: str) -> list[dict]:
    """One item per maintenance failure mode in the complete native output.

    A native tool can exit 0 while its post-transaction hooks failed, so the
    full stdout and stderr are scanned, not the saved tail. Each item is
    {"source": "stdout"|"stderr", "reason": one of MAINTENANCE_REASONS,
    "tool": "pacman"|"rpm"|"modprobe"|"systemd"|"udev",
    "line": first matching line truncated to MAINTENANCE_LINE_LIMIT,
    "count": matching line count}. Only the explicit failure lines below are
    reported; "Skipped: Current root is not booted.", "ignoring" notices,
    dependency output and generic "error:" lines stay quiet.
    """
    if target not in TARGETS:
        return []
    rules = [rule for rule in _MAINTENANCE_RULES if target in rule[2]]
    items: list[dict] = []
    for source, text in (("stdout", stdout or ""), ("stderr", stderr or "")):
        for raw in text.splitlines():
            line = raw.strip()
            if not line or any(marker in line.lower() for marker in _MAINTENANCE_IGNORED):
                continue
            for reason, tool, _targets, pattern in rules:
                if pattern.search(line) is None:
                    continue
                _record_finding(items, source, tool, reason, line)
                break
    return items


def merge_maintenance_findings(items: list[dict], findings: list[dict]) -> list[dict]:
    """Merge one step's findings into the transaction list by failure mode."""
    merged = [dict(item) for item in items or []]
    for finding in findings or []:
        _record_finding(
            merged, finding.get("source"), finding.get("tool"), finding.get("reason"),
            finding.get("line"), count=finding.get("count", 1))
    return merged


def transaction_verdict(phase: str | None, maintenance: dict | None) -> str:
    """Layered status: phase says whether the package was installed.

    Returns "verified", "verified-with-warnings" or "failed". A record written
    before the maintenance field existed has no hook information, so its
    verdict falls back to the phase alone.
    """
    if phase != "completed":
        return "failed"
    maintenance = maintenance or {}
    if maintenance.get("status") == "warnings" or maintenance.get("items"):
        return "verified-with-warnings"
    return "verified"


def _record_finding(items: list[dict], source: str | None, tool: str | None,
                    reason: str | None, line: object, count: int = 1) -> None:
    text = line if isinstance(line, str) else str(line or "")
    truncated = text[:MAINTENANCE_LINE_LIMIT]
    if len(text) > MAINTENANCE_LINE_LIMIT:
        truncated += "..."
    for item in items:
        if (item.get("source"), item.get("tool"), item.get("reason")) == (source, tool, reason):
            item["count"] = int(item.get("count") or 1) + int(count or 1)
            return
    items.append({
        "source": source,
        "reason": reason,
        "tool": tool,
        "line": truncated,
        "count": int(count or 1),
    })


def inventory_has(text: str, name: str) -> bool:
    for line in text.splitlines():
        token = line.strip().split("\t", 1)[0].split(" ", 1)[0]
        if token == name:
            return True
    return False


def local_package_path(target: str, raw: str) -> Path:
    _require_target(target)
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        raise MultilinuxError(f"local package is not a file: {path}")
    if not PACKAGE_NAME.fullmatch(path.name):
        raise MultilinuxError(f"local package file name is not accepted: {path.name}")
    suffixes = LOCAL_SUFFIX[target]
    if not any(path.name.endswith(suffix) for suffix in suffixes):
        expected = ", ".join(suffixes)
        raise MultilinuxError(
            f"{path.name} is not a {target} package; expected a name ending in {expected}")
    return path


def package_fingerprint(text: str) -> dict:
    """SHA-256 of sorted lines. Empty output is a real fingerprint, not an error."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    normalized = ("\n".join(sorted(lines)) + "\n").encode() if lines else b""
    return {
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "count": len(lines),
    }


def host_package_argv(os_id: str) -> list[str] | None:
    if os_id == "fedora":
        return ["rpm", "-qa"]
    if os_id in {"debian", "ubuntu"}:
        return ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"]
    if os_id == "arch":
        return ["pacman", "-Q"]
    return None


def host_os_id() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("ID="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def entry_changes(before: list[str], after: list[str]) -> dict:
    before_set = set(before)
    after_set = set(after)
    return {
        "added": sorted(after_set - before_set),
        "removed": sorted(before_set - after_set),
    }


class HostObserver:
    """Read-only host package set and per-user desktop file names.

    Package bytes are not a promise about the host package manager. The
    transaction compares the sorted fingerprint and the desktop file names
    from before and after the native command.
    """

    def __init__(self, runner=None, os_id: str | None = None, applications: Path | None = None):
        self.runner = runner or _run_host
        self.os_id = host_os_id() if os_id is None else os_id
        self.applications = applications

    def snapshot(self) -> dict:
        return {"packages": self._packages(), "entries": self._entries()}

    def _packages(self) -> dict:
        argv = host_package_argv(self.os_id)
        if argv is None:
            return {
                "available": False,
                "tool": None,
                "sha256": None,
                "count": None,
                "error": f"no host package reader for ID {self.os_id or '(empty)'}",
            }
        try:
            result = self.runner(argv)
        except OSError as exc:
            return {
                "available": False, "tool": argv[0], "sha256": None, "count": None, "error": str(exc),
            }
        if result.returncode != 0:
            detail = ((getattr(result, "stderr", "") or "") + (getattr(result, "stdout", "") or ""))[-500:]
            return {
                "available": False, "tool": argv[0], "sha256": None, "count": None, "error": detail,
            }
        fingerprint = package_fingerprint(result.stdout or "")
        return {
            "available": True,
            "tool": argv[0],
            "sha256": fingerprint["sha256"],
            "count": fingerprint["count"],
            "error": None,
        }

    def _entries(self) -> dict:
        directory = self.applications or (Path.home() / ".local/share/applications")
        names = []
        if directory.is_dir():
            names = sorted(path.name for path in directory.glob("*.desktop") if path.is_file())
        return {"directory": str(directory), "names": names}


def _require_target(target: str) -> None:
    if target not in TARGETS:
        raise MultilinuxError(f"no package adapter for {target}")


def _run_host(argv: list[str]):
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, check=False)
