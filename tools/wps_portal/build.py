#!/usr/bin/env python3
"""Build the WPS 12.1.2.29183 file chooser adapters from pinned local inputs."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile


HERE = Path(__file__).resolve().parent
QT = "5.12.12"
QTBASE = "qtbase-everywhere-src-" + QT
VENDOR_HASHES = {
    "libQt5GuiKso.so.5.12.12": "15d63a17ef0235e2849a24bf2060302672d3a0276e0ca2a7a4b8027862e6ad4d",
    "libQt5CoreKso.so.5.12.12": "d2108676cffc51927bac5978cefe633bbe9b9a7f559aaf6c47327684ec084f63",
    "libQt5WidgetsKso.so.5.12.12": "60023bedd492bf7fde8085d4722e60808d06df238e70e5a5246818955f5a84c4",
    "addons/kcloudfiledialog/libkcloudfiledialog.so": "ab4e719f53ce618fab594c03d9381bd8840f2f8209c528d5ce63bef8505ebc7d",
}
THEME_FILES = (
    "main.cpp", "qxdgdesktopportaltheme.cpp", "qxdgdesktopportaltheme.h",
    "qxdgdesktopportalfiledialog.cpp", "qxdgdesktopportalfiledialog_p.h", "xdgdesktopportal.json",
)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_digest(path, expected):
    if sha256(path) != expected:
        raise ValueError(f"unsupported WPS or Qt input {path}")


def run(*command, cwd=None, timeout=60):
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout)


def extract_theme(archive, source):
    filenames = {"LICENSE.LGPL3": f"{QTBASE}/LICENSE.LGPL3"}
    filenames.update({name: f"{QTBASE}/src/plugins/platformthemes/xdgdesktopportal/{name}"
                      for name in THEME_FILES})
    by_path = {path: name for name, path in filenames.items()}
    with tarfile.open(archive, "r|xz") as stream:
        for member in stream:
            name = by_path.pop(member.name, None)
            if name is None:
                continue
            if not member.isfile() or not 0 < member.size < 100 * 1024:
                raise ValueError(f"unexpected Qt source entry {member.name}")
            with stream.extractfile(member) as data, (source / name).open("xb") as output:
                shutil.copyfileobj(data, output)
            if not by_path:
                return
    raise ValueError("Qt platform theme archive is incomplete")


def build(args):
    sdk_archive, qtbase, vendor, out = (
        path.resolve() for path in (args.qt_sdk, args.qtbase, args.wps_office6, args.out))
    require_digest(sdk_archive, "479f5d51b9c82030102f8f058f82c767d77bf1b64669a282096c9d85829c3a49")
    require_digest(qtbase, "7c15c31590b6ec12408242b1a32075d1d545f35e1d41fdbad31ec670d00680f2")
    for name, digest in VENDOR_HASHES.items():
        require_digest(vendor / name, digest)
    out.mkdir(mode=0o700)
    workspace = out / "build"
    source = workspace / "source"
    source.mkdir(parents=True)
    sdk = workspace / "sdk" / QT / "gcc_64"
    run("7z", "x", "-mmt=1", "-bd", "-y", "-o" + str(workspace / "sdk"), str(sdk_archive))
    extract_theme(qtbase, source)
    qpa = source / "include" / "qpa"
    qpa.mkdir(parents=True)
    stock_qpa = sdk / "include" / "QtGui" / QT / "QtGui" / "qpa"
    for name in ("qplatformtheme.h", "qplatformdialoghelper.h"):
        shutil.copyfile(stock_qpa / name, qpa / name)
    for name, cwd in (("qtbase-theme.patch", source), ("qtbase-qpa.patch", qpa)):
        run("patch", "-p1", "--forward", "--fuzz=0", "--input", str(HERE / name), cwd=cwd)

    generated = workspace / "generated"
    generated.mkdir()
    moc = sdk / "bin" / "moc"
    run(str(moc), "-v", timeout=8)
    headers = [qpa.parent, sdk / "include", sdk / "mkspecs" / "linux-g++"]
    for module in ("QtCore", "QtGui", "QtWidgets", "QtDBus"):
        module_root = sdk / "include" / module
        headers.extend((module_root, module_root / QT, module_root / QT / module))
    flags = ["-DQT_NAMESPACE=kso_qt", "-DQT_NO_DEBUG", *["-I" + str(path) for path in headers]]
    for name, result in (("main.cpp", "main.moc"),
                         ("qxdgdesktopportalfiledialog_p.h", "moc_qxdgdesktopportalfiledialog.cpp")):
        run(str(moc), *flags, str(source / name), "-o", str(generated / result), timeout=12)
    flags.extend(("-I" + str(source), "-I" + str(generated)))
    units = [source / name for name in THEME_FILES if name.endswith(".cpp")]
    units.append(generated / "moc_qxdgdesktopportalfiledialog.cpp")
    objects = []
    for unit in units:
        output = workspace / (unit.stem + ".o")
        run("g++", "-std=c++11", "-fPIC", "-O0", *flags, "-c", str(unit), "-o", str(output))
        objects.append(str(output))

    bundle = out / "bundle"
    platformthemes = bundle / "platformthemes"
    platformthemes.mkdir(parents=True)
    theme = platformthemes / "libqxdgdesktopportal.so"
    run("g++", "-shared", "-Wl,--no-undefined", *objects, "-L" + str(vendor),
        "-Wl,-rpath-link," + str(vendor), "-lQt5GuiKso", "-lQt5DBusKso", "-lQt5CoreKso",
        "-o", str(theme), timeout=20)
    table = subprocess.check_output(["nm", "-D", "-S", "--defined-only", str(theme)],
                                    text=True, timeout=8)
    for name, vtable_size in (("QXdgDesktopPortalTheme", 184), ("QXdgDesktopPortalFileDialog", 240)):
        symbol = f"_ZTVN6kso_qt{len(name)}{name}E"
        sizes = [int(line.split()[1], 16) for line in table.splitlines() if line.endswith(" " + symbol)]
        if sizes != [vtable_size]:
            raise ValueError(f"unexpected Qt vtable {symbol}")

    audit = bundle / "wps-format-audit.so"
    run("gcc", "-std=gnu11", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", "-nostdlib",
        "-fno-builtin", "-fno-stack-protector", "-Wl,-z,defs", str(HERE / "format_audit.c"),
        "-o", str(audit), timeout=10)
    dynamic = subprocess.check_output(["readelf", "-dW", str(audit)], text=True, timeout=5)
    if "(NEEDED)" in dynamic:
        raise ValueError("loader adapter has acquired a shared library dependency")
    manifest = {
        "wps": "12.1.2.29183", "arch": "x86_64", "qt": QT, "vendor_sha256": VENDOR_HASHES,
        "payload_sha256": {str(path.relative_to(bundle)): sha256(path) for path in (theme, audit)},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(source / "LICENSE.LGPL3", bundle / "LICENSE.LGPL3")
    print(bundle)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-sdk", type=Path, required=True, help="the pinned Qt Linux GCC .7z archive")
    parser.add_argument("--qtbase", type=Path, required=True, help="the pinned QtBase source archive")
    parser.add_argument("--wps-office6", type=Path, required=True, help="the installed WPS office6 directory")
    parser.add_argument("--out", type=Path, required=True, help="a new output directory")
    build(parser.parse_args())
