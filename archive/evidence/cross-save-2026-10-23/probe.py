#!/usr/bin/env python3
"""Cross-application save probe against the retained complex-arch container.

The container already has FreeCAD and one shared host directory. This probe
does not install packages, pull images, alter that container's mounts, or
edit the earlier FreeCAD evidence files. It starts the stopped container,
writes only under work/cross-save-2026-10-23/, then stops it again.

A missing historical Xauthority file is recreated empty for the start and
removed afterwards. FreeCAD is invoked through freecadcmd with offscreen Qt
so this probe does not open a desktop window.
"""

import argparse
import ctypes
import ctypes.util
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "evidence/arch-complex-2026-10-22T202359+0800/work"
PROJECT_NAME = "cross-save-2026-10-23"
CONTAINER = "ml-edae1d18-complex-arch"
XAUTH = Path("/run/user/1000/xauth_WtbESS")
SKIP_PREFIX = PROJECT_NAME + "/"

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
IN_CLOEXEC = 0x80000
IN_NONBLOCK = 0x800
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
WATCH_MASK = (IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM
              | IN_MOVED_TO | IN_CREATE | IN_DELETE)
EVENT_NAMES = {
    IN_MODIFY: "modify", IN_ATTRIB: "attrib", IN_CLOSE_WRITE: "close_write",
    IN_MOVED_FROM: "moved_from", IN_MOVED_TO: "moved_to", IN_CREATE: "create",
    IN_DELETE: "delete",
}


class InotifyEvent(ctypes.Structure):
    _fields_ = [
        ("wd", ctypes.c_int), ("mask", ctypes.c_uint32),
        ("cookie", ctypes.c_uint32), ("length", ctypes.c_uint32),
    ]


def run(argv, timeout=60, stdin=None):
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, text=True, capture_output=True, timeout=timeout, input=stdin,
            encoding="utf-8", errors="replace")
        return {"argv": argv, "exit_code": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr, "seconds": round(time.perf_counter() - started, 6),
                "error": None}
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return {"argv": argv, "exit_code": None, "stdout": out, "stderr": err,
                "seconds": round(time.perf_counter() - started, 6), "error": "timeout"}


def clip(text, limit=12000):
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} chars"


def brief(result):
    item = dict(result)
    item["stdout"] = clip(item.get("stdout"))
    item["stderr"] = clip(item.get("stderr"))
    return item


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fact(path):
    path = Path(path)
    try:
        st = path.lstat()
    except OSError as exc:
        return {"path": str(path), "error": exc.__class__.__name__, "errno": exc.errno,
                "message": os.strerror(exc.errno)}
    fact = {"path": str(path), "uid": st.st_uid, "gid": st.st_gid, "mode": oct(st.st_mode),
            "inode": st.st_ino, "size": st.st_size, "is_symlink": path.is_symlink()}
    if path.is_file() and not path.is_symlink():
        fact["sha256"] = sha256(path)
        try:
            fact["text"] = path.read_text(encoding="utf-8")
        except UnicodeError:
            fact["text"] = None
    try:
        fact["selinux"] = os.getxattr(path, "security.selinux").decode().rstrip("\x00")
    except OSError:
        pass
    return fact


def preexisting_hashes(work):
    found = {}
    for path in sorted(work.rglob("*")):
        relative = path.relative_to(work).as_posix()
        if relative == PROJECT_NAME or relative.startswith(SKIP_PREFIX):
            continue
        if path.is_file() and not path.is_symlink():
            found[relative] = sha256(path)
    return found


def read_inotify(fd):
    try:
        blob = os.read(fd, 8192)
    except BlockingIOError:
        return []
    events = []
    offset = 0
    while offset < len(blob):
        event = InotifyEvent.from_buffer_copy(blob, offset)
        offset += ctypes.sizeof(InotifyEvent)
        raw = blob[offset:offset + event.length].split(b"\x00", 1)[0]
        offset += event.length
        names = [EVENT_NAMES[bit] for bit in EVENT_NAMES if event.mask & bit]
        events.append({"mask": names, "cookie": event.cookie, "name": raw.decode("utf-8", "replace")})
    return events


class DirectoryWatch:
    def __init__(self, directory):
        self.directory = str(directory)
        self.events = []
        self.stop = threading.Event()
        self.error = None
        self.fd = None

    def __enter__(self):
        self.fd = libc.inotify_init1(IN_CLOEXEC | IN_NONBLOCK)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        watch = libc.inotify_add_watch(self.fd, os.fsencode(self.directory), WATCH_MASK)
        if watch < 0:
            os.close(self.fd)
            raise OSError(ctypes.get_errno(), "inotify_add_watch")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def _loop(self):
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        while not self.stop.is_set():
            if poller.poll(100):
                self.events.extend(read_inotify(self.fd))

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        self.thread.join(2)
        if self.fd is not None:
            os.close(self.fd)
        return False


def exec_user(command, workdir, timeout=60, extra_env=None):
    argv = ["podman", "exec", "-u", "1000:1000", "-w", str(workdir)]
    for key in ("DISPLAY", "WAYLAND_DISPLAY"):
        argv.extend(["-e", f"{key}="])
    argv.extend(["-e", "QT_QPA_PLATFORM=offscreen"])
    for key, value in (extra_env or {}).items():
        argv.extend(["-e", f"{key}={value}"])
    argv.extend([CONTAINER, *command])
    return brief(run(argv, timeout=timeout))


INSIDE = r'''
import json, os, sys, time
from pathlib import Path

def atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".editor.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.write(fd, data)
    os.fsync(fd)
    os.close(fd)
    before = path.stat().st_ino if path.exists() else None
    os.replace(tmp, path)
    return {"protocol": "temp-fsync-replace", "temp": tmp.name,
            "inode_before": before, "inode_after": path.stat().st_ino}

def inplace(path, data):
    path = Path(path)
    before = path.stat().st_ino
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    os.write(fd, data)
    os.fsync(fd)
    os.close(fd)
    return {"protocol": "truncate-same-fd", "inode_before": before,
            "inode_after": path.stat().st_ino}

def wait_go(ready, go):
    Path(ready).write_text("ready\n")
    while not Path(go).exists():
        time.sleep(0.02)

def stage_replace(path, ready, go):
    path = Path(path)
    stale = path.read_bytes()
    tmp = path.with_name(path.name + ".stale.tmp")
    tmp.write_bytes(stale)
    wait_go(ready, go)
    os.replace(tmp, path)
    return {"protocol": "read-then-replace-after-host", "stale": stale.decode(),
            "final_inode": path.stat().st_ino}

def stage_fd(path, ready, go):
    path = Path(path)
    fd = os.open(path, os.O_RDWR)
    stale = os.read(fd, 4096)
    wait_go(ready, go)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, "容器写到替换前打开的描述符\n".encode())
    os.fsync(fd)
    os.close(fd)
    return {"protocol": "write-old-fd-after-host-replace", "stale": stale.decode(),
            "final_text": path.read_text(), "final_inode": path.stat().st_ino}

def main():
    action = sys.argv[1]
    if action == "atomic":
        result = atomic(sys.argv[2], sys.argv[3].encode())
    elif action == "inplace":
        result = inplace(sys.argv[2], sys.argv[3].encode())
    elif action == "stage-replace":
        result = stage_replace(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "stage-fd":
        result = stage_fd(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "try-inplace":
        try:
            result = inplace(sys.argv[2], sys.argv[3].encode())
            result["error"] = None
        except OSError as exc:
            result = {"protocol": "truncate-same-fd", "error": exc.__class__.__name__,
                      "errno": exc.errno, "message": os.strerror(exc.errno)}
    elif action == "try-atomic":
        try:
            result = atomic(sys.argv[2], sys.argv[3].encode())
            result["error"] = None
        except OSError as exc:
            result = {"protocol": "temp-fsync-replace", "error": exc.__class__.__name__,
                      "errno": exc.errno, "message": os.strerror(exc.errno)}
    else:
        raise SystemExit("unknown action")
    Path(sys.argv[-1]).write_text(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
'''

FREECAD_SAVE = r'''
import json, os
from pathlib import Path
import FreeCAD as App
import Part

project = Path(os.environ["ML_PROJECT"])
doc = App.newDocument("CrossSave")
box = doc.addObject("Part::Box", "Box")
box.Length, box.Width, box.Height = 10, 20, 30
doc.recompute()
model = project / "零件.FCStd"
step = project / "零件.step"
doc.saveAs(str(model))
Part.export([box], str(step))
first_volume = box.Shape.Volume
box.Length = 11
doc.recompute()
doc.save()
report = {
    "filename": doc.FileName,
    "length_after_second_save": box.Length.Value,
    "volume_at_step_export": first_volume,
    "volume_after_second_save": box.Shape.Volume,
    "cwd": os.getcwd(),
    "home": os.environ.get("HOME"),
    "uid": os.getuid(),
}
(project / "freecad-save.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
'''

FREECAD_REOPEN = r'''
import json, os
from pathlib import Path
import FreeCAD as App

project = Path(os.environ["ML_PROJECT"])
doc = App.openDocument(str(project / "零件.FCStd"))
box = doc.getObject("Box")
doc.recompute()
report = {"filename": doc.FileName, "length": box.Length.Value,
          "volume": box.Shape.Volume, "uid": os.getuid()}
(project / "freecad-reopen.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
'''

FREECAD_DEFAULT = r'''
import json, os
from pathlib import Path
import FreeCAD as App

doc = App.newDocument("NoPath")
save_error = None
try:
    doc.save()
except Exception as exc:
    save_error = repr(exc)
relative_error = None
try:
    doc.saveAs("相对零件.FCStd")
    relative_name = doc.FileName
except Exception as exc:
    relative_error = repr(exc)
    relative_name = doc.FileName
home_error = None
home_name = None
try:
    destination = os.path.expanduser("~/默认家目录.FCStd")
    doc2 = App.newDocument("HomeDefault")
    box = doc2.addObject("Part::Box", "Box")
    box.Length, box.Width, box.Height = 1, 1, 1
    doc2.recompute()
    doc2.saveAs(destination)
    home_name = doc2.FileName
except Exception as exc:
    home_error = repr(exc)
report = {"cwd": os.getcwd(), "home": os.environ.get("HOME"), "uid": os.getuid(),
          "save_without_name_error": save_error, "file_after_failed_save": doc.FileName,
          "relative_error": relative_error, "relative_filename": relative_name,
          "home_error": home_error, "home_filename": home_name}
Path(os.environ["ML_REPORT"]).write_text(json.dumps(report, ensure_ascii=False, indent=2))
'''


def coordinated(project, action, filename, initial, host_replacement):
    """Container reads or opens the file, host replaces it, then container continues."""
    path = project / filename
    path.write_text(initial)
    ready = project / f"{filename}.ready"
    go = project / f"{filename}.go"
    for flag in (ready, go):
        if flag.exists():
            flag.unlink()
    result_path = project / f"{filename}.result.json"
    command = ["python3", str(project / "inside.py"), action, str(path), str(ready), str(go),
               str(result_path)]
    with DirectoryWatch(project) as watch:
        proc = subprocess.Popen(
            ["podman", "exec", "-u", "1000:1000", "-w", str(project),
             "-e", "DISPLAY=", "-e", "WAYLAND_DISPLAY=", "-e", "QT_QPA_PLATFORM=offscreen",
             CONTAINER, *command],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
        deadline = time.time() + 30
        while not ready.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.02)
        host_tmp = project / f"{filename}.host.tmp"
        host_tmp.write_text(host_replacement)
        os.replace(host_tmp, path)
        host_inode = path.stat().st_ino
        go.write_text("go\n")
        try:
            stdout, stderr = proc.communicate(timeout=30)
            code = proc.returncode
            error = None
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            code = None
            error = "timeout"
    inside = json.loads(result_path.read_text()) if result_path.exists() else None
    return {"action": action, "container": {"exit_code": code, "stdout": clip(stdout),
                                             "stderr": clip(stderr), "error": error},
            "inside": inside, "host_inode_after_replace": host_inode,
            "final": file_fact(path), "events": watch.events}


def load_json(path):
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; use a new name")
    project = WORK / PROJECT_NAME
    report = {
        "time": dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "purpose": "host project saved by Arch FreeCAD and an editor-style replace",
        "product_acceptance": False,
        "container": CONTAINER,
        "project": str(project),
        "preexisting_sha256": preexisting_hashes(WORK),
    }
    created_xauth = False
    started = False
    try:
        state = run(["podman", "inspect", CONTAINER, "--format", "{{.State.Status}}"])
        report["state_before"] = state["stdout"].strip() if state["exit_code"] == 0 else state
        if report["state_before"] != "exited":
            report["error"] = "container was not stopped; left untouched"
            return finish(args.output, report)
        mounts = run(["podman", "inspect", CONTAINER, "--format", "{{json .Mounts}}"])
        report["share_present"] = str(WORK) in mounts["stdout"]
        if not report["share_present"]:
            report["error"] = "expected work share is not on this container"
            return finish(args.output, report)
        if not XAUTH.exists():
            fd = os.open(XAUTH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created_xauth = True
        report["xauth_placeholder"] = created_xauth
        start = run(["podman", "start", CONTAINER], timeout=40)
        report["start"] = brief(start)
        if start["exit_code"] != 0:
            report["error"] = "start failed"
            return finish(args.output, report)
        started = True
        project.mkdir(mode=0o755, exist_ok=True)
        (project / "inside.py").write_text(INSIDE)
        (project / "freecad_save.py").write_text(FREECAD_SAVE)
        (project / "freecad_reopen.py").write_text(FREECAD_REOPEN)
        (project / "freecad_default.py").write_text(FREECAD_DEFAULT)
        note = project / "笔记.txt"
        side = project / "旁注.txt"
        note.write_text("宿主原文 v1\n")
        side.write_text("相邻说明不要动\n")
        side_before = file_fact(side)
        outside = (args.output.parent / "outside-marker.txt").resolve()
        outside.write_text("容器不应看见\n")
        report["outside_visibility"] = exec_user(
            ["sh", "-c", f'test -e {str(outside)!r} && echo visible || echo absent'], project, timeout=20)

        with DirectoryWatch(project) as watch:
            report["atomic_save"] = exec_user(
                ["python3", str(project / "inside.py"), "atomic", str(note),
                 "Arch 编辑器协议保存 v2\n", str(project / "atomic.json")],
                project, timeout=30)
            report["atomic_events"] = list(watch.events)
        report["atomic_inside"] = load_json(project / "atomic.json")
        report["note_after_atomic"] = file_fact(note)
        report["side_after_atomic"] = file_fact(side)

        with DirectoryWatch(project) as watch:
            report["inplace_save"] = exec_user(
                ["python3", str(project / "inside.py"), "inplace", str(note),
                 "同 inode 覆盖 v3\n", str(project / "inplace.json")],
                project, timeout=30)
            report["inplace_events"] = list(watch.events)
        report["inplace_inside"] = load_json(project / "inplace.json")
        report["note_after_inplace"] = file_fact(note)

        readonly = project / "只读笔记.txt"
        readonly.write_text("只读原文\n")
        os.chmod(readonly, 0o444)
        report["readonly_inplace"] = exec_user(
            ["python3", str(project / "inside.py"), "try-inplace", str(readonly),
             "不应覆盖\n", str(project / "readonly-inplace.json")],
            project, timeout=30)
        report["readonly_inplace_inside"] = load_json(project / "readonly-inplace.json")
        report["readonly_atomic"] = exec_user(
            ["python3", str(project / "inside.py"), "try-atomic", str(readonly),
             "目录可写所以替换成功\n", str(project / "readonly-atomic.json")],
            project, timeout=30)
        report["readonly_atomic_inside"] = load_json(project / "readonly-atomic.json")
        report["readonly_after"] = file_fact(readonly)

        report["race_replace"] = coordinated(
            project, "stage-replace", "竞争-替换.txt", "容器读到的原文\n",
            "宿主在暂存之后替换的内容\n")
        report["race_fd"] = coordinated(
            project, "stage-fd", "竞争-描述符.txt", "描述符打开时的原文\n",
            "宿主换了新 inode\n")
        report["side_after_races"] = file_fact(side)
        report["side_unchanged"] = report["side_after_races"].get("sha256") == side_before.get("sha256")

        with DirectoryWatch(project) as watch:
            report["freecad_save"] = exec_user(
                ["freecadcmd", str(project / "freecad_save.py")], project, timeout=180,
                extra_env={"ML_PROJECT": str(project)})
            report["freecad_save_events"] = list(watch.events)
        report["freecad_save_report"] = load_json(project / "freecad-save.json")
        report["fcstd"] = file_fact(project / "零件.FCStd")
        report["step"] = file_fact(project / "零件.step")
        report["freecad_reopen"] = exec_user(
            ["freecadcmd", str(project / "freecad_reopen.py")], project, timeout=180,
            extra_env={"ML_PROJECT": str(project)})
        report["freecad_reopen_report"] = load_json(project / "freecad-reopen.json")

        default_report = project / "freecad-default.json"
        report["freecad_default_at_root"] = exec_user(
            ["freecadcmd", str(project / "freecad_default.py")], "/", timeout=180,
            extra_env={"ML_REPORT": str(default_report), "HOME": "/home/multilinux-1000"})
        report["freecad_default_report"] = load_json(default_report)
        home_file = "/home/multilinux-1000/默认家目录.FCStd"
        report["home_default_inside"] = exec_user(
            ["sh", "-c", f'test -f {home_file} && sha256sum {home_file} || echo absent'],
            project, timeout=20, extra_env={"HOME": "/home/multilinux-1000"})
        report["home_default_visible_in_project"] = (project / "默认家目录.FCStd").exists()
        report["relative_at_root_visible_in_project"] = (project / "相对零件.FCStd").exists()
        report["remove_home_default"] = exec_user(["rm", "-f", home_file], project, timeout=20)
        names = sorted(path.name for path in project.iterdir())
        report["project_names"] = names
        return finish(args.output, report)
    except Exception as exc:
        report["error"] = repr(exc)
        return finish(args.output, report)
    finally:
        if started:
            report["stop"] = brief(run(["podman", "stop", "-t", "0", CONTAINER], timeout=40))
            after = run(["podman", "inspect", CONTAINER, "--format", "{{.State.Status}}"])
            report["state_after"] = after["stdout"].strip() if after["exit_code"] == 0 else after
        report["preexisting_sha256_after"] = preexisting_hashes(WORK)
        report["preexisting_unchanged"] = (
            report.get("preexisting_sha256_after") == report.get("preexisting_sha256"))
        if created_xauth and XAUTH.exists():
            XAUTH.unlink()
            report["xauth_removed"] = not XAUTH.exists()
        if args.output.exists():
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def finish(output, report):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "error": report.get("error"),
        "state_before": report.get("state_before"),
        "preexisting_unchanged": report.get("preexisting_unchanged"),
        "side_unchanged": report.get("side_unchanged"),
        "atomic": report.get("atomic_inside"),
        "inplace": report.get("inplace_inside"),
        "readonly_inplace": report.get("readonly_inplace_inside"),
        "readonly_atomic": report.get("readonly_atomic_inside"),
        "race_replace_final": (report.get("race_replace") or {}).get("final"),
        "race_fd_final": (report.get("race_fd") or {}).get("final"),
        "freecad_save": report.get("freecad_save_report"),
        "freecad_reopen": report.get("freecad_reopen_report"),
        "freecad_default": report.get("freecad_default_report"),
        "freecad_save_exit": (report.get("freecad_save") or {}).get("exit_code"),
        "project_names": report.get("project_names"),
    }, ensure_ascii=False, indent=2))
    return 0 if not report.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
