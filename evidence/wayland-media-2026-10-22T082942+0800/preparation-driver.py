#!/usr/bin/env python3
"""Native Wayland, clipboard, audio and local Debian package acceptance.

All work uses a new persistent project snapshot and native package managers.
The previous XWayland experiment and the main project's old environments are
not started or modified. Installed environments are retained stopped.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.probe_desktop_apps import prepare, save, service_for, sha, log, need
from multilinux.service import now

PACKAGES = {
    "debian13": ["mousepad", "wl-clipboard", "dbus", "python3-gi", "gir1.2-gtk-3.0",
                 "pulseaudio-utils", "fonts-noto-cjk"],
    "fedora44": ["mousepad", "wl-clipboard", "dbus-daemon", "python3-gobject",
                 "pulseaudio-utils", "google-noto-sans-cjk-vf-fonts"],
    "arch": ["mousepad", "wl-clipboard", "dbus", "python-gobject", "gtk3", "libpulse",
             "noto-fonts-cjk"],
}


def prepare_native(run_dir):
    manifest = prepare(run_dir)
    manifest["scope"] = (
        "Native GTK/Wayland windows, host/environment clipboard exchange, "
        "real playback streams on the current host audio output, and a local "
        "Tencent Meeting deb in an independent Debian environment. No meeting/login acceptance."
    )
    manifest["package_sources"] = {}
    manifest["preparation_driver_sha256"] = sha(__file__)
    shutil.copyfile(__file__, run_dir / "preparation-driver.py")
    service = service_for(run_dir)
    for target, packages in PACKAGES.items():
        env_id = "native-" + target
        directory = run_dir / target
        directory.mkdir(exist_ok=True)
        docs = run_dir / "documents" / target
        docs.mkdir(parents=True, exist_ok=True)
        record = manifest["targets"].setdefault(target, {})
        record.update(environment_id=env_id, packages=packages, preparation_started_at=now())
        save(run_dir / "manifest.json", manifest)
        try:
            if not service.store.exists(env_id):
                service.create(target, env_id, [str(docs)], "pasta", desktop=True)
            log(f"{target}: installing native Wayland editor and media/clipboard tools")
            txn = service.install_packages(env_id, packages, use_argv=["mousepad", "--version"])
            save(directory / "install-transaction.json", txn)
            record["install_transaction"] = txn["id"]
            need(txn["phase"] == "completed", f"install failed: {txn.get('native_error')} {txn.get('verification')}")
            shutil.copyfile(service.store.inventory_path(env_id), directory / "packages.txt")
            record["package_inventory_sha256"] = sha(directory / "packages.txt")
            record["prepared"] = True
            save(directory / "environment-prepared.json", service.inspect(env_id))
            log(f"{target}: preparation passed")
        except Exception as exc:
            record["prepared"] = False
            record["preparation_error"] = f"{type(exc).__name__}: {exc}"
            log(f"{target}: preparation failed: {exc}")
        finally:
            if service.store.exists(env_id):
                service.stop(env_id)
            record["preparation_finished_at"] = now()
            save(run_dir / "manifest.json", manifest)
    manifest["preparation_passed"] = all(x.get("prepared") for x in manifest["targets"].values())
    save(run_dir / "manifest.json", manifest)
    return 0 if manifest["preparation_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%dT%H%M%S%z")
    run_dir = (args.run_dir or ROOT / "evidence" / ("wayland-media-" + stamp)).resolve()
    log(f"native acceptance directory: {run_dir}")
    return prepare_native(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
