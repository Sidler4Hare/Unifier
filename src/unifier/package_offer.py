"""Desktop package handoff that stays outside the host's own package format.

Double-click preparation is only for a package Unifier already routes and the
host whitelist allows as a cross-family guest. A Fedora rpm, a Debian deb, or
an Arch package on its own family never opens this page; it is handed back to
the host opener that was current before registration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from unifier.catalog import PREPARED
from unifier.compatibility import classify_host, relationship_plan
from unifier.desktop import desktop_exec_quote
from unifier.errors import UnifierError


DESKTOP_ID = "unifier-package-offer.desktop"
ARCH_MIME = "application/x-multilinux-archpkg"
FORMAT_MIMES = {
    "deb": ("application/vnd.debian.binary-package", "application/x-deb"),
    "rpm": ("application/x-rpm",),
    "arch": (ARCH_MIME,),
}
HOST_DESKTOPS = (
    "org.gnome.Software.desktop",
    "org.kde.discover.desktop",
    "org.kde.ark.desktop",
    "org.gnome.FileRoller.desktop",
)
DESKTOP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]*\.desktop\Z")
STATE_NAME = "package-offer.json"


def host_package_format(facts: dict) -> str | None:
    return classify_host(facts).get("package_format")


def whitelist_formats(facts: dict) -> list[str]:
    """Prepared formats this host may intercept. The host's own format is absent."""
    native = host_package_format(facts)
    found = []
    for target, spec in PREPARED.items():
        if not relationship_plan(facts, target)["allowed"]:
            continue
        fmt = spec["package_format"]
        if fmt == native or fmt in found:
            continue
        found.append(fmt)
    return found


def mimes_for(formats: list[str]) -> list[str]:
    found = []
    for fmt in formats:
        for mime in FORMAT_MIMES[fmt]:
            if mime not in found:
                found.append(mime)
    return found


def admission(route: dict, facts: dict) -> dict:
    """Decide whether this file may open the preparation page."""
    fmt = route.get("format")
    native = host_package_format(facts)
    if fmt == native:
        return {"enter": False, "reason": "host-native", "format": fmt, "targets": []}
    allowed = []
    for target in route.get("candidates") or []:
        if relationship_plan(facts, target)["allowed"]:
            allowed.append(target)
    if not allowed:
        return {"enter": False, "reason": "outside-whitelist", "format": fmt, "targets": []}
    return {"enter": True, "reason": "cross-family", "format": fmt, "targets": allowed}


def parse_choice(answer: str) -> str:
    value = answer.strip().lower()
    if value in {"y", "yes"}:
        return "unifier"
    if value in {"n", "no"}:
        return "host"
    raise UnifierError("package offer choice must be y or n")


def validate_desktop_id(desktop_id: str) -> str:
    if not isinstance(desktop_id, str) or not DESKTOP_NAME.fullmatch(desktop_id):
        raise UnifierError("host package handler name is invalid")
    if desktop_id == DESKTOP_ID:
        raise UnifierError("refusing to hand the package back to Unifier")
    return desktop_id


def select_host_desktop(saved: str | None, available: set[str]) -> str | None:
    if saved and saved in available and saved != DESKTOP_ID and DESKTOP_NAME.fullmatch(saved):
        return saved
    for name in HOST_DESKTOPS:
        if name in available:
            return name
    return None


def host_launch_argv(desktop_id: str, path: str, *, tool: str = "gio") -> list[str]:
    validate_desktop_id(desktop_id)
    if tool not in {"gio", "gtk-launch"}:
        raise UnifierError("unsupported host launcher")
    if tool == "gio":
        return ["gio", "launch", desktop_id, path]
    return ["gtk-launch", desktop_id, path]


def prefer_mime(text: str | None, mime: str, desktop_id: str) -> tuple[str, str | None]:
    """Put our handler first for one mime and return the previous handler."""
    lines = [] if text is None else text.splitlines()
    previous = None
    section = None
    rewritten = []
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "Default Applications" and not inserted:
                rewritten.append(f"{mime}={desktop_id}")
                inserted = True
            section = stripped[1:-1].strip()
            rewritten.append(line)
            continue
        if section == "Default Applications" and stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip() == mime:
                current = value.strip().split(";", 1)[0].strip()
                if current and current != desktop_id and DESKTOP_NAME.fullmatch(current):
                    previous = current
                if not inserted:
                    rewritten.append(f"{mime}={desktop_id}")
                    inserted = True
                continue
        rewritten.append(line)
    if not inserted:
        if rewritten and rewritten[-1] != "":
            rewritten.append("")
        if section != "Default Applications":
            rewritten.extend(["[Default Applications]", f"{mime}={desktop_id}"])
        else:
            rewritten.append(f"{mime}={desktop_id}")
    body = "\n".join(rewritten)
    return (body if body.endswith("\n") else body + "\n"), previous


def restore_mime(text: str | None, mime: str, previous: str | None) -> str:
    lines = [] if text is None else text.splitlines()
    kept = []
    section = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
        if section == "Default Applications" and stripped.startswith(mime + "="):
            continue
        kept.append(line)
    if previous:
        validate_desktop_id(previous)
        body, _ignored = prefer_mime("\n".join(kept) + ("\n" if kept else ""), mime, previous)
        return body
    meaningful = [line.strip() for line in kept if line.strip()]
    if meaningful in ([], ["[Default Applications]"]):
        return ""
    body = "\n".join(kept)
    return body if body.endswith("\n") else body + "\n"


def desktop_entry(launcher: str, mimes: list[str]) -> str:
    if not mimes:
        raise UnifierError("package offer has no foreign whitelist formats on this host")
    mime_line = ";".join(mimes) + ";"
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=Unifier Package",
        "Name[zh_CN]=Unifier 安装管理",
        "Comment=Open a foreign package in Unifier or hand it back to the host",
        f"Exec={desktop_exec_quote(launcher)} offer %f",
        "Terminal=true",
        "NoDisplay=true",
        f"MimeType={mime_line}",
        "Categories=System;",
        "",
    ])


def arch_mime_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-multilinux-archpkg">
    <comment>Arch package routed by Unifier</comment>
    <glob pattern="*.pkg.tar.zst"/>
    <glob pattern="*.pkg.tar.xz"/>
    <glob pattern="*.pkg.tar.gz"/>
  </mime-type>
</mime-info>
"""


def state_path(root: Path) -> Path:
    return Path(root) / STATE_NAME


def load_state(root: Path) -> dict:
    path = state_path(root)
    if not path.is_file():
        return {"schema_version": 1, "previous": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise UnifierError("package offer state is invalid")
    if not isinstance(data.get("previous", {}), dict):
        raise UnifierError("package offer state is invalid")
    return data


def save_state(root: Path, data: dict) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def launcher_path() -> Path:
    return Path(__file__).resolve().parents[2] / "bin" / "unifier"


def register_text(existing: str | None, mimes: list[str], stored: dict) -> tuple[str, dict]:
    text = existing
    previous = dict(stored)
    for mime in mimes:
        text, found = prefer_mime(text, mime, DESKTOP_ID)
        if mime not in previous:
            previous[mime] = found if found != DESKTOP_ID else None
    return text, previous


def unregister_text(existing: str | None, previous: dict) -> str:
    text = existing or ""
    for mime, saved in previous.items():
        text = restore_mime(text, mime, saved)
    return text


def write_registration(root: Path, desktop_dir: Path, mime_dir: Path, *,
                       facts: dict, launcher: str, query_default) -> list[str]:
    formats = whitelist_formats(facts)
    if not formats:
        raise UnifierError("this host has no foreign package format on the Unifier whitelist")
    mimes = mimes_for(formats)
    stored = load_state(root).get("previous") or {}
    mimeapps = Path(desktop_dir) / "mimeapps.list"
    existing = mimeapps.read_text(encoding="utf-8") if mimeapps.is_file() else None
    text, previous = register_text(existing, mimes, stored)
    for mime in mimes:
        if previous.get(mime):
            continue
        queried = query_default(mime)
        if queried and queried != DESKTOP_ID and DESKTOP_NAME.fullmatch(queried):
            previous[mime] = queried
    desktop_dir.mkdir(parents=True, exist_ok=True)
    (Path(desktop_dir) / DESKTOP_ID).write_text(desktop_entry(launcher, mimes), encoding="utf-8")
    xml = Path(mime_dir) / "packages" / "unifier-archpkg.xml"
    if "arch" in formats:
        xml.parent.mkdir(parents=True, exist_ok=True)
        xml.write_text(arch_mime_xml(), encoding="utf-8")
    elif xml.is_file():
        xml.unlink()
    mimeapps.write_text(text, encoding="utf-8")
    save_state(root, {"schema_version": 1, "formats": formats, "previous": previous,
                      "launcher": launcher})
    return formats


def remove_registration(root: Path, desktop_dir: Path, mime_dir: Path) -> None:
    state = load_state(root)
    mimeapps = Path(desktop_dir) / "mimeapps.list"
    existing = mimeapps.read_text(encoding="utf-8") if mimeapps.is_file() else ""
    mimeapps.write_text(unregister_text(existing, state.get("previous") or {}), encoding="utf-8")
    desktop = Path(desktop_dir) / DESKTOP_ID
    if desktop.is_file() or desktop.is_symlink():
        desktop.unlink()
    xml = Path(mime_dir) / "packages" / "unifier-archpkg.xml"
    if xml.is_file():
        xml.unlink()
    path = state_path(root)
    if path.is_file():
        path.unlink()
