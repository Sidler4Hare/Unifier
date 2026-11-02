"""One real GUI editor on the current host session.

Opt in with MULTILINUX_INTEGRATION=1. The run installs gvim and its libraries
in a Debian environment, mounts the current Wayland, X11 and audio sockets,
and checks open, edit, save and a second open. It does not measure performance
and does not repeat the scenario on Fedora or Arch.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux.desktop import desktop_id
from multilinux.packages import HostObserver
from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime, container_name, project_label
from multilinux.service import EnvironmentService, now


ENV_ID = "m3debian"
APP_ID = "m3note"
DOCS = Path("/tmp/ml-m3-docs")
NOTE_NAME = "note.mlnote"
SAVED = "multilinux-desktop-saved"
PACKAGES = ("vim-gtk3", "x11-utils", "pulseaudio-utils", "python3")


def _reset(service, runtime):
    project = project_label(service.root)
    name = container_name(service.root, ENV_ID)
    if service._launch_exists(APP_ID):
        service.remove_app(APP_ID)
    if service.store.exists(ENV_ID):
        service.remove(ENV_ID)
        return
    info = runtime.inspect(name)
    if info is not None and info.project == project and info.env_id == ENV_ID:
        runtime.remove(name, project=project, env_id=ENV_ID)


def _brief(txn):
    return {
        "id": txn["id"],
        "operation": txn["operation"],
        "phase": txn["phase"],
        "failure_phase": txn.get("failure_phase"),
        "native_error": txn.get("native_error"),
        "native": [
            {
                "argv": step.get("argv"),
                "exit_code": step.get("exit_code"),
                "stdout_tail": (step.get("stdout_tail") or "")[-500:],
                "stderr_tail": (step.get("stderr_tail") or "")[-500:],
            }
            for step in txn.get("native") or []
        ],
    }


@unittest.skipUnless(os.environ.get("MULTILINUX_INTEGRATION") == "1",
                     "set MULTILINUX_INTEGRATION=1 to open a GUI application")
class PodmanDesktop(unittest.TestCase):
    def test_gvim_opens_edits_saves_and_opens_again(self):
        config, lock, root = load_project()
        runtime = PodmanRuntime()
        service = EnvironmentService(root, runtime, config, lock)
        observer = HostObserver()
        host_before = observer.snapshot()
        mimeapps = service.host_desktop.mimeapps
        mime_before = mimeapps.read_bytes() if mimeapps.is_file() else None
        names_before = list(host_before["entries"]["names"])
        note = DOCS / NOTE_NAME
        evidence = {
            "schema_version": 1,
            "started_at": now(),
            "host_kernel": os.uname().release,
            "experimental_package": "vim-gtk3",
            "host_packages_before": host_before["packages"],
            "desktop_entries_before": names_before,
            "passed": False,
            "scope": (
                "M3 desktop session on the current Fedora host. "
                "One Debian GUI editor. Not a performance measurement or another host."
            ),
        }
        proc = None
        try:
            if DOCS.exists():
                shutil.rmtree(DOCS)
            DOCS.mkdir()
            note.write_text("original\n", encoding="utf-8")
            shutil.copy(ROOT / "fixtures/wayland_handshake.py", DOCS / "wayland_handshake.py")
            _reset(service, runtime)
            record = service.create("debian13", ENV_ID, [str(DOCS)], "pasta", desktop=True)
            evidence["desktop"] = {
                "selinux_label": record["runtime"]["selinux_label"],
                "reason": record["desktop"]["reason"],
                "env": record["desktop"]["env"],
                "mounts": record["desktop"]["mounts"],
                "network": record["runtime"]["network"],
            }
            self.assertEqual(record["runtime"]["selinux_label"], "disable")
            self.assertIn("WAYLAND_DISPLAY", record["desktop"]["env"])
            self.assertIn("DISPLAY", record["desktop"]["env"])
            self.assertIn("PULSE_SERVER", record["desktop"]["env"])

            installed = service.install_packages(ENV_ID, list(PACKAGES))
            evidence["install"] = _brief(installed)
            self.assertEqual(installed["phase"], "completed", json.dumps(evidence["install"]))
            gvim = self._executable(service, ("/usr/bin/gvim", "/usr/bin/vim.gtk3"))
            evidence["gvim"] = gvim
            service.add_app(
                ENV_ID, APP_ID, gvim,
                [gvim, "-N", "-n", "-u", "NONE", "-U", "NONE", "--servername", "MLNOTE", "--nofork"],
                workdir=str(DOCS), env_overlay={"GDK_BACKEND": "x11"})
            version = service.exec_app(
                APP_ID, [gvim, "--version"], capture=True, timeout=30)
            evidence["gvim_version"] = (version.stdout or version.stderr).splitlines()[:2]

            wayland = service.exec_app(
                APP_ID, ["python3", str(DOCS / "wayland_handshake.py")],
                capture=True, timeout=20)
            evidence["wayland"] = {
                "exit_code": wayland.returncode,
                "stdout": wayland.stdout.strip(),
                "stderr": wayland.stderr.strip()[-500:],
            }
            self.assertEqual(wayland.returncode, 0, wayland.stderr)
            self.assertIn("wayland-reply-bytes=", wayland.stdout)

            audio = service.exec_app(APP_ID, ["pactl", "info"], capture=True, timeout=20)
            evidence["audio"] = {
                "exit_code": audio.returncode,
                "stdout": audio.stdout[-800:],
                "stderr": audio.stderr[-400:],
            }
            self.assertEqual(audio.returncode, 0, audio.stderr)
            self.assertIn("PipeWire", audio.stdout)

            launch = service.export_app(APP_ID)
            desktop_text = Path(launch["export"]["desktop_path"]).read_text(encoding="utf-8")
            evidence["desktop_exec"] = [
                line for line in desktop_text.splitlines() if line.startswith("Exec=")
            ][0]
            self.assertIn("app open m3note --", evidence["desktop_exec"])
            self.assertNotIn(gvim, evidence["desktop_exec"])
            queried = subprocess.run(
                ["xdg-mime", "query", "default", "application/x-multilinux-note"],
                check=False, capture_output=True, text=True)
            evidence["mime_default"] = queried.stdout.strip()
            self.assertEqual(evidence["mime_default"], Path(launch["export"]["desktop_path"]).name,
                             queried.stderr)

            proc, log_path = self._open(note)
            window = self._wait_for_window(service, gvim)
            evidence["window"] = window
            self.assertTrue(window, self._log_tail(log_path))
            edited = service.exec_app(APP_ID, [
                gvim, "--servername", "MLNOTE", "--remote-send",
                '<Esc>:call setline(1, "multilinux-desktop-saved")<CR>:w<CR>',
            ], capture=True, timeout=30)
            self.assertEqual(edited.returncode, 0, edited.stderr or edited.stdout)
            self._wait_for_text(note, SAVED)
            self.assertEqual(note.read_text(encoding="utf-8").splitlines()[0], SAVED)
            quit_first = service.exec_app(APP_ID, [
                gvim, "--servername", "MLNOTE", "--remote-send", "<Esc>:qa!<CR>",
            ], capture=True, timeout=30)
            self.assertEqual(quit_first.returncode, 0, quit_first.stderr)
            self.assertEqual(proc.wait(timeout=30), 0, self._log_tail(log_path))
            proc = None

            proc, log_path = self._open(note)
            self.assertTrue(self._wait_for_window(service, gvim), self._log_tail(log_path))
            reread = service.exec_app(
                APP_ID, [gvim, "--servername", "MLNOTE", "--remote-expr", "getline(1)"],
                capture=True, timeout=30)
            evidence["reread"] = (reread.stdout or "").strip()
            self.assertEqual(reread.returncode, 0, reread.stderr or reread.stdout)
            self.assertIn(SAVED, evidence["reread"])
            self.assertEqual(note.read_text(encoding="utf-8").splitlines()[0], SAVED)
            quit_second = service.exec_app(APP_ID, [
                gvim, "--servername", "MLNOTE", "--remote-send", "<Esc>:qa!<CR>",
            ], capture=True, timeout=30)
            self.assertEqual(quit_second.returncode, 0, quit_second.stderr)
            self.assertEqual(proc.wait(timeout=30), 0, self._log_tail(log_path))
            proc = None

            during = observer.snapshot()
            evidence["desktop_entries_added"] = sorted(
                set(during["entries"]["names"]) - set(names_before))
            self.assertTrue(evidence["desktop_entries_added"])
            service.unexport_app(APP_ID)
            service.remove_app(APP_ID)
            after = observer.snapshot()
            self.assertEqual(after["entries"]["names"], names_before)
            self.assertEqual(after["packages"]["sha256"], host_before["packages"]["sha256"])
            self.assertEqual(after["packages"]["count"], host_before["packages"]["count"])
            mime_after = mimeapps.read_bytes() if mimeapps.is_file() else None
            self.assertEqual(mime_after, mime_before)
            evidence["host_packages_after"] = after["packages"]
            evidence["host_packages_unchanged"] = True
            evidence["desktop_entries_restored"] = True
            evidence["mimeapps_restored"] = True
            evidence["passed"] = True
            evidence["finished_at"] = now()
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            with self._quiet():
                if service._launch_exists(APP_ID):
                    service.remove_app(APP_ID)
                _reset(service, runtime)
            if DOCS.exists():
                shutil.rmtree(DOCS, ignore_errors=True)
            name = desktop_id(root, APP_ID)
            (service.host_desktop.applications / f"{name}.desktop").unlink(missing_ok=True)
            (service.host_desktop.mime_packages / f"{name}.xml").unlink(missing_ok=True)
            if mime_before is None:
                mimeapps.unlink(missing_ok=True)
            else:
                mimeapps.parent.mkdir(parents=True, exist_ok=True)
                mimeapps.write_bytes(mime_before)
            with self._quiet():
                service.host_desktop.refresh()
            if mimeapps.is_file() or mime_before is not None:
                if mime_before is None:
                    mimeapps.unlink(missing_ok=True)
                elif not mimeapps.is_file() or mimeapps.read_bytes() != mime_before:
                    mimeapps.write_bytes(mime_before)
        if evidence["passed"]:
            self.assertFalse(service.store.exists(ENV_ID))
            self.assertIsNone(runtime.inspect(container_name(root, ENV_ID)))
            evidence["containers_removed_after_acceptance"] = True
            evidence["cleaned_at"] = now()
            if os.environ.get("MULTILINUX_EVIDENCE") == "1":
                from tools import lab
                lab.save_json(root / "evidence/m3-desktop-2026-10-19.json", evidence)

    def _executable(self, service, candidates):
        service.start(ENV_ID)
        record = service.inspect(ENV_ID)
        runtime = record["runtime"]
        for candidate in candidates:
            result = service.runtime.exec(
                runtime["container_name"], ["test", "-x", candidate],
                project=runtime["project"], env_id=ENV_ID, workdir=None, env={},
                capture=True, timeout=20)
            if result.returncode == 0:
                return candidate
        self.fail("gvim was not installed at " + ", ".join(candidates))

    def _open(self, note: Path):
        log_path = Path("/tmp/m3-gvim.log")
        log = log_path.open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "tools/ml.py"), "app", "open", APP_ID, "--", str(note)],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        finally:
            log.close()
        return proc, log_path

    def _wait_for_window(self, service, gvim):
        deadline = time.time() + 30
        last = ""
        while time.time() < deadline:
            result = service.exec_app(
                APP_ID, ["xwininfo", "-root", "-tree"], capture=True, timeout=20)
            last = result.stdout or result.stderr
            for line in last.splitlines():
                if NOTE_NAME in line or "MLNOTE" in line:
                    return line.strip()
            if result.returncode != 0 and "unable to open display" in last.lower():
                return ""
            time.sleep(1)
        return ""

    def _wait_for_text(self, note: Path, expected: str):
        deadline = time.time() + 10
        while time.time() < deadline:
            if note.is_file() and expected in note.read_text(encoding="utf-8"):
                return
            time.sleep(0.25)
        self.fail(f"saved text was not written; file is {note.read_text(encoding='utf-8')!r}")

    def _log_tail(self, path: Path) -> str:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-1500:]

    class _quiet:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return True


if __name__ == "__main__":
    unittest.main()
