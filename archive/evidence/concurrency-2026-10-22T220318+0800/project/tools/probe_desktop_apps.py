#!/usr/bin/env python3
"""Exercise a real gVim window in all three native user spaces.

Uses a persistent, isolated copy of the project so old session-bound
environments are never started by package-isolation checks. Successful
environments are left installed and stopped for inspection or another open.
The GUI is controlled through gVim's own client/server interface, not a
headless editor. Screenshots capture only the application's X11 window.
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux.desktop import discover_session
from multilinux.packages import HostObserver
from multilinux.project import load_project
from multilinux.resources import ResourceProbe, host_snapshot
from multilinux.runtime import PodmanRuntime, project_token
from multilinux.service import EnvironmentService, now


TARGETS = {
    "debian13": ("vim-gtk3", "fonts-noto-cjk"),
    "fedora44": ("vim-X11", "google-noto-sans-cjk-vf-fonts"),
    "arch": ("gvim", "noto-fonts-cjk"),
}
STATE_EXPR = (
    "json_encode({'pid':getpid(),'window_id':v:windowid,"
    "'gui_running':has('gui_running'),'path':expand('%:p'),"
    "'lines':getline(1,'$'),'modified':&modified,"
    "'encoding':&encoding,'fileencoding':&fileencoding})"
)


def log(message):
    print(f"[{now()}] {message}", flush=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def source_paths():
    return [
        *sorted(ROOT.glob("src/multilinux/*.py")),
        ROOT / "tools/ml.py", ROOT / "tools/lab.py",
        ROOT / "config/targets.toml", ROOT / "config/images.lock.json",
    ]


def protected_files():
    """Fingerprint the pre-existing project state without starting it."""
    state = ROOT / ".lab/state"
    paths = [*state.glob("environments/*"), *state.glob("launches/*")]
    return {str(p.relative_to(ROOT)): sha(p) for p in sorted(paths) if p.is_file()}


def mime_state():
    path = Path.home() / ".local/share/applications/mimeapps.list"
    return {"path": str(path), "exists": path.is_file(),
            "sha256": sha(path) if path.is_file() else None}


def prepare(run_dir):
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        for rel, expected in manifest["source_sha256"].items():
            need(sha(ROOT / rel) == expected, f"source changed since snapshot: {rel}")
            need(sha(run_dir / "project" / rel) == expected, f"snapshot changed: {rel}")
        return manifest
    run_dir.mkdir(parents=True, exist_ok=False)
    snapshot = run_dir / "project"
    hashes = {}
    for source in source_paths():
        rel = source.relative_to(ROOT)
        destination = snapshot / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        hashes[str(rel)] = sha(source)
        need(sha(destination) == hashes[str(rel)], f"snapshot copy mismatch: {rel}")
    manifest = {
        "schema_version": 1, "started_at": now(), "source_root": str(ROOT),
        "project_root": str(snapshot), "source_sha256": hashes,
        "host_before": HostObserver().snapshot(), "host_resources_before": host_snapshot(),
        "original_project_state_before": protected_files(), "mime_before": mime_state(),
        "session": discover_session(), "targets": {}, "passed": False,
        "scope": "Three native gVim packages on one Fedora host, GTK3/X11 on XWayland. "
                 "Automated real-window edits, save and reopen after a full environment stop. "
                 "No human mouse/keyboard, native Wayland GUI, clipboard, audio playback, GPU, "
                 "service/init or cross-host acceptance is implied.",
        "package_sources": {
            "fedora44": "https://packages.fedoraproject.org/pkgs/vim/vim-X11/",
            "arch": "https://archlinux.org/packages/extra/x86_64/gvim/",
        },
    }
    save(manifest_path, manifest)
    return manifest


def service_for(run_dir):
    config, lock, _ = load_project()
    return EnvironmentService(run_dir / "project", PodmanRuntime(), config, lock)


def check_mounts(record):
    mounts = record["shared_mounts"] + record["desktop"]["mounts"]
    missing = [m["host"] for m in mounts if not Path(m["host"]).exists()]
    need(not missing, f"session/share mount sources are missing; create a new run: {missing}")


def app_command(service, app_id, argv):
    result = service.exec_app(app_id, argv, capture=True, timeout=30)
    need(result.returncode == 0,
         f"application command failed ({result.returncode}): {(result.stderr or result.stdout)[-1500:]}")
    return result.stdout.strip()


def expression(service, app_id, gvim, server, expr):
    return app_command(service, app_id, [gvim, "--servername", server, "--remote-expr", expr])


def send(service, app_id, gvim, server, keys):
    return app_command(service, app_id, [gvim, "--servername", server, "--remote-send", keys])


def wait_state(service, app_id, gvim, server, process):
    deadline = time.monotonic() + 45
    last = ""
    while time.monotonic() < deadline:
        need(process.poll() is None, f"GUI launcher exited before a live window (exit {process.returncode})")
        try:
            state = json.loads(expression(service, app_id, gvim, server, STATE_EXPR))
            if state["gui_running"] == 1 and state["window_id"] > 0:
                return state
            last = str(state)
        except (RuntimeError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.3)
    raise RuntimeError(f"no responsive GUI window after 45s: {last}")


def screenshot(window_id, path):
    completed = subprocess.run(
        ["magick", "import", "-silent", "-window", str(window_id), "-strip", str(path)],
        capture_output=True, text=True, timeout=30)
    need(completed.returncode == 0, f"window capture failed: {completed.stderr}")
    need(path.is_file() and path.stat().st_size > 1000, "window screenshot is empty")
    return {"path": path.name, "sha256": sha(path), "bytes": path.stat().st_size,
            "window_id": window_id, "capture": "X11 application window only"}


def launch(run_dir, app_id, note, log_path):
    with log_path.open("w") as stream:
        return subprocess.Popen(
            [sys.executable, str(run_dir / "project/tools/ml.py"), "app", "open", app_id,
             "--", str(note)], cwd=run_dir / "project", stdout=stream, stderr=subprocess.STDOUT)


def sample(probe, env_id, directory, name):
    result = probe.sample(env_id)
    save(directory / (name + ".json"), result)
    if result["runtime_state"] == "running":
        need(result["cgroup"] is not None, f"missing cgroup in {name}")
        need(result["processes"]["pss_bytes"] is not None, f"missing PSS in {name}")
    return result


def run_target(run_dir, manifest, target):
    service = service_for(run_dir)
    probe = ResourceProbe(service)
    target_dir = run_dir / target
    target_dir.mkdir(exist_ok=True)
    attempts = manifest["targets"].setdefault(target, {"attempts": []})["attempts"]
    directory = target_dir / f"attempt-{len(attempts) + 1}"
    directory.mkdir()
    shutil.copyfile(__file__, directory / "driver.py")
    env_id, app_id = "gui-" + target, "editor-" + target
    server = "ML" + project_token(service.root).upper() + target.upper()
    docs = run_dir / "documents" / target
    docs.mkdir(parents=True, exist_ok=True)
    note = docs / ("实测 note " + target + ".mlnote")
    initial = f"Original host document for {target}\n"
    expected_lines = [
        f"Multilinux GUI acceptance: {target}",
        "中文编辑保存与重开验证：你好，Linux。",
        'UTF-8, spaces and quotes: "saved by the GUI"',
        "Reopened after stopping and starting the environment.",
    ]
    expected_bytes = ("\n".join(expected_lines) + "\n").encode("utf-8")
    if note.exists():
        shutil.copyfile(note, directory / "document-before-attempt.mlnote")
    note.write_text(initial, encoding="utf-8")
    attempt = {
        "started_at": now(), "target": target, "environment_id": env_id,
        "application_id": app_id, "server": server, "document": str(note),
        "packages": list(TARGETS[target]), "passed": False, "stage": "create",
        "driver_sha256": sha(__file__), "errors": [], "checks": {},
    }
    attempts.append(attempt)
    save(run_dir / "manifest.json", manifest)
    process = None
    try:
        log(f"{target}: creating current-session environment")
        if not service.store.exists(env_id):
            service.create(target, env_id, [str(docs)], "pasta", desktop=True)
        record = service.inspect(env_id)
        check_mounts(record)
        save(directory / "environment-before.json", record)
        attempt["stage"] = "install"
        save(directory / "result.json", attempt)
        installed = service.install_packages(env_id, list(TARGETS[target]),
                                              use_argv=["/usr/bin/gvim", "--version"])
        save(directory / "install-transaction.json", installed)
        attempt["install_transaction"] = installed["id"]
        need(installed["phase"] == "completed",
             f"native install failed: {installed.get('native_error')} {installed.get('verification')}")
        inventory = service.store.inventory_path(env_id)
        shutil.copyfile(inventory, directory / "packages.txt")
        attempt["package_inventory_sha256"] = sha(inventory)
        for package in TARGETS[target]:
            save(directory / (package + "-query.json"), service.query_package(env_id, package))
        gvim = "/usr/bin/gvim"
        if not service._launch_exists(app_id):
            service.add_app(
                env_id, app_id, gvim,
                [gvim, "-N", "-n", "-i", "NONE", "-u", "NONE", "-U", "NONE",
                 "--servername", server, "--nofork", "-geometry", "100x22+40+80",
                 "--cmd", "set encoding=utf-8", "-c", "set guifont=Monospace\\ 12"],
                workdir=str(docs), env_overlay={"GDK_BACKEND": "x11", "LC_ALL": "C.UTF-8"})
        save(directory / "launch-record.json", service.show_app(app_id))
        version = app_command(service, app_id, [gvim, "--version"])
        (directory / "gvim-version.txt").write_text(version + "\n")
        need("+clientserver" in version, "gVim has no client/server support")
        fonts = app_command(service, app_id, ["fc-match", "-f", "%{family}\n", "monospace:lang=zh-cn"])
        (directory / "font-match.txt").write_text(fonts + "\n")
        attempt["version"] = version.splitlines()[0]
        attempt["font"] = fonts
        sample(probe, env_id, directory, "idle-before-open")

        attempt["stage"] = "first-open"
        log(f"{target}: opening the real editor window")
        process = launch(run_dir, app_id, note, directory / "first-open.log")
        opened = wait_state(service, app_id, gvim, server, process)
        need(opened["path"] == str(note), "first-open document path mismatch")
        need(opened["lines"] == initial.splitlines(), "GUI did not read the host's original text")
        attempt["first_open"] = opened

        attempt["stage"] = "edit-and-save"
        edited_text = json.dumps(expected_lines, ensure_ascii=False)
        send(service, app_id, gvim, server, "<Esc>:call setline(1, " + edited_text + ")<CR>")
        edited = json.loads(expression(service, app_id, gvim, server, STATE_EXPR))
        need(edited["lines"] == expected_lines and edited["modified"] == 1,
             "editor buffer did not become modified with the requested content")
        need(note.read_text(encoding="utf-8") == initial, "file changed before the editor's save command")
        attempt["edited_before_save"] = edited
        send(service, app_id, gvim, server, "<Esc>:set fileencoding=utf-8<CR>:write<CR>")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and note.read_bytes() != expected_bytes:
            time.sleep(0.1)
        need(note.read_bytes() == expected_bytes, "saved host file bytes differ from expected UTF-8")
        saved = json.loads(expression(service, app_id, gvim, server, STATE_EXPR))
        need(saved["modified"] == 0, "editor still reports unsaved changes")
        need(note.stat().st_uid == os.getuid() and note.stat().st_gid == os.getgid(),
             "saved file is not owned by the host user")
        attempt["saved"] = saved
        attempt["saved_sha256"] = sha(note)
        attempt["saved_owner"] = {"uid": note.stat().st_uid, "gid": note.stat().st_gid}
        attempt["saved_screenshot"] = screenshot(saved["window_id"], directory / "saved.png")
        sample(probe, env_id, directory, "gui-saved")
        send(service, app_id, gvim, server, "<Esc>:qa<CR>")
        need(process.wait(timeout=30) == 0, "first GUI launcher did not exit cleanly")
        process = None
        service.stop(env_id)
        stopped = sample(probe, env_id, directory, "stopped-between-opens")
        need(stopped["cgroup"] is None and stopped["runtime_state"] == "stopped",
             "environment was not stopped between the two opens")

        attempt["stage"] = "reopen-after-environment-restart"
        log(f"{target}: reopening after a full environment stop")
        process = launch(run_dir, app_id, note, directory / "reopen.log")
        reopened = wait_state(service, app_id, gvim, server, process)
        need(reopened["path"] == str(note), "reopened document path mismatch")
        need(reopened["lines"] == expected_lines and reopened["modified"] == 0,
             "reopened GUI buffer differs from the saved document")
        need(note.read_bytes() == expected_bytes, "document bytes changed on reopen")
        attempt["reopened"] = reopened
        attempt["reopened_sha256"] = sha(note)
        attempt["reopened_screenshot"] = screenshot(reopened["window_id"], directory / "reopened.png")
        sample(probe, env_id, directory, "gui-reopened")
        send(service, app_id, gvim, server, "<Esc>:qa<CR>")
        need(process.wait(timeout=30) == 0, "second GUI launcher did not exit cleanly")
        process = None
        sample(probe, env_id, directory, "idle-after-close")
        service.stop(env_id)
        sample(probe, env_id, directory, "stopped-final")
        final = service.inspect(env_id)
        save(directory / "environment-final.json", final)
        attempt["checks"] = {
            "native_install": True, "live_gui_window": True, "host_file_initial_read": True,
            "buffer_modified_before_save": True, "host_file_unchanged_before_save": True,
            "exact_utf8_save": True, "host_file_owner": True, "first_clean_exit": True,
            "environment_stopped_between_opens": True, "reopened_buffer_matches": True,
            "reopened_sha256_matches": True, "second_clean_exit": True,
            "installed_environment_retained_stopped": final["runtime_state"] == "stopped",
        }
        attempt["passed"] = all(attempt["checks"].values())
        attempt["stage"] = "completed"
        log(f"{target}: PASS — installed, opened, edited, saved, stopped and reopened")
    except Exception as exc:
        attempt["errors"].append(f"{type(exc).__name__}: {exc}")
        (directory / "failure-traceback.txt").write_text(traceback.format_exc())
        log(f"{target}: FAIL at {attempt['stage']}: {exc}")
    finally:
        if service.store.exists(env_id):
            try:
                service.stop(env_id)
            except Exception as exc:
                attempt["errors"].append(f"stop failed: {exc}")
                attempt["passed"] = False
        if process is not None:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
        attempt["finished_at"] = now()
        attempt["artifacts"] = str(directory.relative_to(run_dir))
        save(directory / "result.json", attempt)
        save(run_dir / "manifest.json", manifest)
    return attempt


def write_report(run_dir, manifest):
    rows = []
    for target, entry in manifest["targets"].items():
        latest = entry["attempts"][-1]
        state = "通过" if latest["passed"] else "失败：" + latest["stage"]
        rel = latest["artifacts"]
        rows.append(f"| {target} | {', '.join(latest['packages'])} | {state} | "
                    f"[记录]({rel}/result.json) / [重开窗口]({rel}/reopened.png) |")
    lines = [
        "# 三环境真实图形应用实测", "", f"时间：{manifest['started_at']} 至 {manifest.get('finished_at', '运行中')}。", "",
        "测试在一台 Fedora 宿主的当前 Wayland 会话进行，三套环境串行使用各自仓库的 gVim。"
        "窗口通过 GTK3/X11 与 XWayland 显示，由 gVim 自身的客户端接口自动编辑和保存。", "",
        "| 环境 | 原生包 | 结果 | 证据 |", "|---|---|---|---|", *rows, "",
        "每套流程检查宿主原始文件读取、保存前缓冲区修改与磁盘未变、UTF-8 精确保存、"
        "宿主 UID/GID、正常退出、整个环境停止，以及重新启动后的缓冲区内容和文件 SHA-256。"
        "路径含中文和空格，内容含中文与引号。截图只捕获编辑器窗口。", "",
        "环境与启动记录保留在独立源码快照中，流程结束后停止。再次打开：", "", "```bash",
    ]
    for target in manifest["targets"]:
        lines.append(f"python3 tools/probe_desktop_apps.py open --run-dir '{run_dir}' --target {target}")
    lines += ["```", "", "当前会话结束后认证路径可能失效，届时需创建新一轮环境。旧项目环境保持原状。",
              "本轮不覆盖人工鼠标键盘交互、原生 Wayland GUI、剪贴板、音频播放、GPU、完整 init/服务、"
              "跨宿主或性能排名。资源快照分别记录 cgroup 和 PSS，不相加。", "",
              "原生包名参考 [Fedora 官方包页](https://packages.fedoraproject.org/pkgs/vim/vim-X11/) 和 "
              "[Arch 官方包页](https://archlinux.org/packages/extra/x86_64/gvim/)，实际安装版本以每环境包清单为准。", "",
              "[机器可读总记录](manifest.json)包含源码校验、镜像身份、事务和宿主隔离核对。", ""]
    (run_dir / "report.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run or retry native GUI acceptance; retain stopped environments")
    run.add_argument("--run-dir", type=Path, help="new persistent evidence directory, or an existing run to retry")
    run.add_argument("--target", choices=tuple(TARGETS), action="append")
    opened = commands.add_parser("open", help="open a retained editor for the current desktop session")
    opened.add_argument("--run-dir", type=Path, required=True)
    opened.add_argument("--target", choices=tuple(TARGETS), required=True)
    args = parser.parse_args()
    if args.command == "open":
        run_dir = args.run_dir.resolve()
        service = service_for(run_dir)
        record = service.inspect("gui-" + args.target)
        check_mounts(record)
        note = run_dir / "documents" / args.target / ("实测 note " + args.target + ".mlnote")
        return service.open_app("editor-" + args.target, [str(note)]).returncode
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%dT%H%M%S%z")
    run_dir = (args.run_dir or ROOT / "evidence" / ("gui-apps-" + stamp)).resolve()
    manifest = prepare(run_dir)
    log(f"evidence and isolated source snapshot: {run_dir}")
    for target in args.target or TARGETS:
        run_target(run_dir, manifest, target)
        write_report(run_dir, manifest)
    after = HostObserver().snapshot()
    manifest["host_after"] = after
    manifest["host_resources_after"] = host_snapshot()
    manifest["original_project_state_after"] = protected_files()
    manifest["mime_after"] = mime_state()
    manifest["isolation"] = {
        "host_packages_unchanged": after["packages"] == manifest["host_before"]["packages"],
        "desktop_entries_unchanged": after["entries"] == manifest["host_before"]["entries"],
        "mimeapps_unchanged": manifest["mime_after"] == manifest["mime_before"],
        "original_project_state_unchanged": manifest["original_project_state_after"] == manifest["original_project_state_before"],
    }
    manifest["finished_at"] = now()
    manifest["passed"] = all(entry["attempts"][-1]["passed"] for entry in manifest["targets"].values()) and all(manifest["isolation"].values())
    save(run_dir / "manifest.json", manifest)
    write_report(run_dir, manifest)
    log(f"overall {'PASS' if manifest['passed'] else 'FAIL'}; report: {run_dir / 'report.md'}")
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
