"""Release payload and guest entry point for the version-bound WPS picker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


MODE = "wps-portal"
ENTRY = "/usr/bin/wps"
OFFICE6 = Path("/opt/kingsoft/wps-office/office6")
ASSETS = Path(__file__).with_name("wps_portal_assets")
THEME = "platformthemes/libqxdgdesktopportal.so"
AUDIT = "wps-format-audit.so"
VENDOR_SHA256 = {
    "libQt5GuiKso.so.5.12.12": "15d63a17ef0235e2849a24bf2060302672d3a0276e0ca2a7a4b8027862e6ad4d",
    "libQt5CoreKso.so.5.12.12": "d2108676cffc51927bac5978cefe633bbe9b9a7f559aaf6c47327684ec084f63",
    "libQt5WidgetsKso.so.5.12.12": "60023bedd492bf7fde8085d4722e60808d06df238e70e5a5246818955f5a84c4",
    "addons/kcloudfiledialog/libkcloudfiledialog.so": "ab4e719f53ce618fab594c03d9381bd8840f2f8209c528d5ce63bef8505ebc7d",
}


def is_writer(record, executable):
    return record.get("target") == "debian13" and executable == ENTRY


def payload_available():
    return (ASSETS / "manifest.json").is_file()


def _manifest(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalid WPS adapter manifest")
    hashes = value.get("payload_sha256")
    if (value.get("wps") != "12.1.2.29183" or value.get("arch") != "x86_64"
            or value.get("qt") != "5.12.12" or value.get("vendor_sha256") != VENDOR_SHA256
            or not isinstance(hashes, dict) or set(hashes) != {THEME, AUDIT}):
        raise ValueError("unsupported WPS adapter manifest")
    return hashes


def payload():
    data = (ASSETS / "manifest.json").read_bytes()
    launcher = Path(__file__).read_bytes()
    name = "wps-portal-" + hashlib.sha256(data + b"\0" + launcher).hexdigest()[:16]
    files = {}
    for key, expected in _manifest(data).items():
        with (ASSETS / key).open("rb") as stream:
            body = stream.read(1024 * 1024 + 1)
        if (len(body) > 1024 * 1024 or not body.startswith(b"\x7fELF\x02\x01")
                or hashlib.sha256(body).hexdigest() != expected):
            raise ValueError("WPS adapter payload verification failed")
        files[key] = body
    return name, launcher, data, files


def _verified_hash(path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 64 * 1024 * 1024:
            return False
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat(follow_symlinks=False)
    return (digest == expected
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))


def guest_environment(bundle, vendor, environ):
    hashes = _manifest((bundle / "manifest.json").read_bytes())
    if (environ.get("LD_AUDIT")
            or not all(_verified_hash(vendor / name, digest) for name, digest in VENDOR_SHA256.items())
            or not all(_verified_hash(bundle / name, digest) for name, digest in hashes.items())):
        raise ValueError("WPS files or the launch environment do not match the KDE picker adapter")
    paths = [str(bundle)]
    if previous := environ.get("QT_PLUGIN_PATH"):
        paths.append(previous)
    return {**environ, "QT_QPA_PLATFORMTHEME": "xdgdesktopportal",
            "QT_PLUGIN_PATH": ":".join(paths), "LD_AUDIT": str(bundle / AUDIT),
            "UNIFIER_WPS_PICKER_EMIT_FILTER": "1"}


def main(argv):
    if len(argv) < 2 or argv[:2] != ["--", ENTRY]:
        raise SystemExit("Unifier WPS adapter expects /usr/bin/wps")
    env = os.environ.copy()
    try:
        env = guest_environment(Path(__file__).parent, OFFICE6, env)
    except (OSError, ValueError) as exc:
        print(f"unifier: {exc}. WPS will use its own file picker.", file=sys.stderr)
    os.execve(ENTRY, argv[1:], env)


if __name__ == "__main__":
    main(sys.argv[1:])
