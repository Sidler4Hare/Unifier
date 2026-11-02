#!/usr/bin/env python3
"""Build tiny native packages for CLI acceptance; never install on the host."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

NAME = "unifier-cli-probe"
SCRIPT = b"""#!/bin/sh
set -eu
printf 'UNIFIER_CLI_PROBE\\n'
printf '%s\\n' "$@"
"""


def tar_bytes(items):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content, mode in items:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(content), mode, 0
            info.uid, info.gid = 0, 0
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(stream.getvalue(), mtime=0)


def ar_member(name, content):
    header = (
        f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}"
        + chr(96) + "\n"
    ).encode("ascii")
    assert len(header) == 60
    return header + content + (b"\n" if len(content) % 2 else b"")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    files = [
        ("usr/bin/unifier-cli-probe", SCRIPT, 0o755),
        ("usr/bin/unifier-cli-probe-two", SCRIPT, 0o755),
    ]
    control = (
        "Package: unifier-cli-probe\nVersion: 1.0-1\nArchitecture: all\n"
        "Maintainer: Unifier acceptance <noreply@example.invalid>\n"
        "Description: Deterministic CLI acceptance fixture\n"
    ).encode()
    deb = out / (NAME + "_1.0-1_all.deb")
    deb.write_bytes(
        b"!<arch>\n"
        + ar_member("debian-binary", b"2.0\n")
        + ar_member("control.tar.gz", tar_bytes([("control", control, 0o644)]))
        + ar_member("data.tar.gz", tar_bytes(files))
    )
    pkginfo = (
        "pkgname = unifier-cli-probe\npkgbase = unifier-cli-probe\n"
        "pkgver = 1.0-1\npkgdesc = Deterministic CLI acceptance fixture\n"
        "url = https://example.invalid/\nbuilddate = 0\n"
        "packager = Unifier acceptance\nsize = 1024\narch = any\nlicense = MIT\n"
    ).encode()
    arch = out / (NAME + "-1.0-1-any.pkg.tar.gz")
    arch.write_bytes(tar_bytes([(".PKGINFO", pkginfo, 0o644), *files]))
    rpmdir = out / "rpm-build"
    for part in ["BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"]:
        (rpmdir / part).mkdir(parents=True)
    script = rpmdir / "SOURCES/probe"
    script.write_bytes(SCRIPT)
    spec = rpmdir / "SPECS/probe.spec"
    spec.write_text(
        "Name: unifier-cli-probe\nVersion: 1.0\nRelease: 1\n"
        "Summary: Deterministic CLI acceptance fixture\nLicense: MIT\n"
        "BuildArch: noarch\nAutoReqProv: no\nSource0: probe\n"
        "%description\nDeterministic CLI acceptance fixture.\n"
        "%prep\n%build\n%install\n"
        "mkdir -p %{buildroot}/usr/bin\n"
        "install -m 0755 %{SOURCE0} %{buildroot}/usr/bin/unifier-cli-probe\n"
        "install -m 0755 %{SOURCE0} %{buildroot}/usr/bin/unifier-cli-probe-two\n"
        "%files\n/usr/bin/unifier-cli-probe\n/usr/bin/unifier-cli-probe-two\n"
    )
    result = subprocess.run(
        ["rpmbuild", "--define", "_topdir " + str(rpmdir),
         "--define", "_build_id_links none", "-bb", str(spec)],
        text=True, capture_output=True, check=False,
    )
    (out / "rpmbuild.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit("RPM fixture build failed; see rpmbuild.log")
    rpms = list((rpmdir / "RPMS").rglob("*.rpm"))
    assert len(rpms) == 1
    rpm = out / rpms[0].name
    rpm.write_bytes(rpms[0].read_bytes())
    manifest = {
        "name": NAME,
        "executables": ["/usr/bin/unifier-cli-probe", "/usr/bin/unifier-cli-probe-two"],
        "host_install_performed": False,
        "packages": {
            target: {
                "path": str(path), "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for target, path in [("debian13", deb), ("fedora44", rpm), ("arch", arch)]
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"output": str(out), "packages": 3, "host_install_performed": False}))


if __name__ == "__main__":
    main()
