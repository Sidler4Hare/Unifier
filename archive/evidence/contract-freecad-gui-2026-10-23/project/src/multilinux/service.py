"""Environment lifecycle above a runtime adapter.

The service owns the durable record. The runtime owns the container. A record
is written only after the container's distribution, kernel and package
inventory have been checked. A failed create removes the container it just
created and leaves no record.
"""

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

from multilinux.catalog import plan_change, resolve_target_contract, system_contract
from multilinux.errors import MultilinuxError
from multilinux.packages import (
    INCOMING_DIR,
    TIMEOUT_PACMAN_KEY,
    HostObserver,
    driver_for,
    entry_changes,
    explain_native_error,
    inventory_argv,
    inventory_has,
    local_package_path,
    maintenance_findings,
    merge_maintenance_findings,
    native_env,
    query_argv,
    transaction_verdict,
    validate_package_name,
    validate_use_argv,
)
from multilinux.desktop import (
    MIME_TYPE,
    HostDesktop,
    add_mime_association,
    desktop_id,
    discover_session,
    remove_mime_association,
    render_desktop_entry,
    render_mime_xml,
    validate_app_id,
    validate_desktop_session,
)
from multilinux.runtime import (
    ROLE_ENVIRONMENT,
    ContainerState,
    CreateSpec,
    ExecResult,
    container_name,
    project_label,
)
from multilinux.store import EnvironmentStore


ENV_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
# Mounting these, or anything inside them, would hide the environment's own
# system tree. /home itself is too broad; a specific directory under it is allowed.
SYSTEM_PREFIXES = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/opt", "/proc",
    "/root", "/run", "/sbin", "/sys", "/usr", "/var",
)
# A graceful podman stop may take up to its own 8 s timeout. Lifecycle calls
# that meet a "stopping" container wait for that transition instead of racing
# podman start or treating a half-stopped container as released.
LIFECYCLE_SETTLE_SECONDS = 15.0
LIFECYCLE_SETTLE_POLL_SECONDS = 0.05


def now() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _log_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _syu_succeeded(native: list) -> bool:
    return any(
        (step.get("argv") or [])[:2] == ["pacman", "-Syu"] and step.get("exit_code") == 0
        for step in native)


def _keyring_ready(native: list) -> bool:
    """True only after the keyring commands queued for this transaction succeeded."""
    init = [step for step in native if (step.get("argv") or [])[:2] == ["pacman-key", "--init"]]
    if not init or init[-1].get("exit_code") != 0:
        return False
    populate = [step for step in native
                if (step.get("argv") or [])[:2] == ["pacman-key", "--populate"]]
    return not populate or populate[-1].get("exit_code") == 0


def _package_view(snapshot: dict) -> dict:
    return {
        "available": bool(snapshot.get("available")),
        "tool": snapshot.get("tool"),
        "sha256": snapshot.get("sha256"),
        "count": snapshot.get("count"),
        "error": snapshot.get("error"),
    }


def _compare_others(before: dict, after: dict) -> dict:
    compared = {}
    for env_id in sorted(set(before) | set(after)):
        left = before.get(env_id) or {}
        right = after.get(env_id) or {}
        error = right.get("error") or left.get("error")
        unchanged = (
            error is None
            and left.get("stored") == right.get("stored")
            and left.get("probed") == left.get("stored")
            and right.get("probed") == right.get("stored")
        )
        compared[env_id] = {
            "before": left.get("stored"),
            "after": right.get("stored"),
            "probed_before": left.get("probed"),
            "probed_after": right.get("probed"),
            "unchanged": unchanged,
            "error": error,
        }
    return compared


def _isolation_problems(txn: dict) -> list[str]:
    problems = []
    others = txn.get("other_environments") or {}
    if "_error" in others and len(others) == 1:
        problems.append(str(others["_error"]))
    else:
        for env_id, item in others.items():
            if item.get("error"):
                problems.append(f"{env_id}: {item['error']}")
            elif not item.get("unchanged", False):
                problems.append(f"{env_id} inventory changed")
    host = txn.get("host_packages") or {}
    before = host.get("before") or {}
    after = host.get("after") or {}
    if not before.get("available") or not after.get("available"):
        problems.append("host package observer unavailable")
    elif before.get("sha256") != after.get("sha256"):
        problems.append("host package set changed")
    entries = txn.get("host_entries") or {}
    if entries.get("added") or entries.get("removed"):
        problems.append("host desktop entries changed")
    return problems


def _hides_system_tree(path: str) -> bool:
    if path in {"/", "/home"}:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in SYSTEM_PREFIXES)


def validate_env_id(env_id: str) -> str:
    if not ENV_ID.fullmatch(env_id) or env_id.endswith("-"):
        raise MultilinuxError(
            "environment id must be 1-32 characters, start with a lowercase letter, "
            "and contain only lowercase letters, digits and hyphens")
    return env_id


def parse_os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def normalize_inventory(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise MultilinuxError("package inventory is empty")
    return "\n".join(sorted(lines)) + "\n"


class EnvironmentService:
    def __init__(self, root: Path, runtime, config: dict, lock: dict, *,
                 host_kernel: str | None = None, clock=None, uid: int | None = None,
                 gid: int | None = None, host_observer=None, log=None,
                 host_desktop=None, cache_runner=None):
        self.root = Path(root)
        self.runtime = runtime
        self.config = config
        self.lock = lock
        self.host_kernel = host_kernel if host_kernel is not None else os.uname().release
        self.clock = clock or now
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid
        self.store = EnvironmentStore(self.root)
        self.host_observer = host_observer if host_observer is not None else HostObserver()
        self.host_desktop = host_desktop if host_desktop is not None else HostDesktop()
        self.cache_runner = cache_runner
        self.log = log or _log_progress

    def create(self, target_name: str, env_id: str | None, shares: list[str],
               network: str = "none", desktop: bool = False, session: dict | None = None,
               user_namespace: str = "rootless-default", devices: list[str] | None = None,
               portals: bool = False, gpu: bool = False, ipc: str | None = None) -> dict:
        target = self._target(target_name)
        resolve_target_contract(target_name, target)
        if network not in {"none", "pasta"}:
            raise MultilinuxError("environment network must be none or pasta")
        if user_namespace not in {"rootless-default", "keep-id"}:
            raise MultilinuxError("user namespace must be rootless-default or keep-id")
        if session is not None and not desktop:
            raise MultilinuxError("a session snapshot requires --desktop")
        if (devices or portals or gpu) and not desktop:
            raise MultilinuxError("camera/gpu devices and desktop portals require --desktop")
        for device in devices or []:
            if not re.fullmatch(r"(/dev/video[0-9]+|/dev/dri/(card|renderD)[0-9]+)", device) or not (
                os.path.exists(device) and stat.S_ISCHR(os.stat(device).st_mode)
            ):
                raise MultilinuxError("desktop device must be an existing /dev/videoN camera or /dev/dri device")
        effective_devices = list(devices or [])
        if gpu:
            dri_path = Path("/dev/dri")
            if dri_path.exists():
                for p in sorted(dri_path.iterdir()):
                    if (p.name.startswith("renderD") or p.name.startswith("card")) and stat.S_ISCHR(p.stat().st_mode):
                        s = str(p)
                        if s not in effective_devices:
                            effective_devices.append(s)
        env_id = validate_env_id(env_id or target_name)
        desktop_session = None
        session_mounts = ()
        selinux_label = "default"
        if desktop:
            desktop_session = validate_desktop_session(session or discover_session())
            if portals:
                directory = self.root.resolve() / '.lab/desktop' / env_id
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
                inside = '/run/multilinux-portal'
                # The private directory contains only this app bridge and a copy
                # of X authentication, never the host's whole runtime directory.
                desktop_session['mounts'] = [m for m in desktop_session['mounts']
                                            if m['container'] != '/tmp/ml-xauthority']
                desktop_session['mounts'].append({'host': str(directory), 'container': inside, 'options': ''})
                desktop_session['env'].update(DBUS_SESSION_BUS_ADDRESS=f'unix:path={inside}/bus',
                                               XAUTHORITY=f'{inside}/Xauthority')
                desktop_session['portal_proxy'] = {'directory': str(directory), 'container': inside}
            session_mounts = tuple((item["host"], item["container"]) for item in desktop_session["mounts"])
            selinux_label = desktop_session["selinux_label"]
        if self.store.exists(env_id):
            raise MultilinuxError(f"environment {env_id} already exists")
        image = self._locked_reference(target_name)
        mounts = tuple(self._shares(shares))
        spec = CreateSpec(
            name=container_name(self.root, env_id),
            env_id=env_id,
            project=project_label(self.root),
            image=image,
            mounts=mounts,
            network=network,
            session_mounts=session_mounts,
            selinux_label=selinux_label,
            user_namespace=user_namespace,
            devices=tuple(effective_devices),
            ipc="host" if desktop else (ipc or "private"),
        )
        existing = self.runtime.inspect(spec.name)
        if existing is not None:
            raise MultilinuxError(
                f"container {spec.name} already exists; this project will not replace it")
        self.runtime.create(spec)
        try:
            self.runtime.start(spec.name, project=spec.project, env_id=env_id)
            identity = self._read_identity(spec)
            self._validate_identity(target_name, target, identity)
            inventory = self._read_inventory(spec, target_name)
            self.runtime.stop(spec.name, project=spec.project, env_id=env_id)
            info = self._require_owned_record(spec.name, spec.project, env_id)
            record = self._record(spec, target_name, target, image, identity, inventory, info,
                                  desktop_session)
            self.store.save(record, inventory)
            return record
        except Exception:
            with contextlib.suppress(Exception):
                self.runtime.remove(spec.name, project=spec.project, env_id=env_id)
            raise

    def list_summaries(self) -> list[dict]:
        summaries = []
        for record in self.store.all():
            view = self._live(record)
            summaries.append({
                "id": view["id"],
                "target": view["target"],
                "distribution_id": view["distribution_id"],
                "version_track": view["version_track"],
                "runtime_state": view["runtime_state"],
                "writable_revision": view["writable_revision"],
                "base_image_digest": view["base_image"]["manifest_digest"],
                "kernel_matches_current_host": view["kernel_matches_current_host"],
            })
        return summaries

    def inspect(self, env_id: str) -> dict:
        return self._live(self.store.load(validate_env_id(env_id)))

    def change_plan(self, env_id: str, desired: dict | None = None) -> dict:
        """Say whether a requested change upgrades this environment or replaces it.

        This does not create, start or modify the environment. Package content
        stays an in-place upgrade. Frozen identity fields come back as replace.
        """
        record = self.store.load(validate_env_id(env_id))
        desired = dict(desired or {})
        if "shares" in desired:
            desired["shares"] = tuple(sorted(self._plan_share(item) for item in desired["shares"]))
        if "desktop" in desired:
            desired.setdefault("selinux_label", "disable" if desired["desktop"] else "default")
            desired.setdefault("ipc", "host" if desired["desktop"] else "private")
        if "target" in desired:
            spec = resolve_target_contract(desired["target"], self._target(desired["target"]))
            desired["image_digest"] = self._lock_digest(desired["target"])
            for key in ("family", "libc", "layout", "package_driver", "package_format",
                        "runtime_backend"):
                desired[key] = spec[key]
        elif not desired:
            current = (record.get("base_image") or {}).get("manifest_digest")
            locked = self._lock_digest(record["target"]) if record.get("target") in self.lock["targets"] else None
            if locked and current and locked != current:
                desired["image_digest"] = locked
        return plan_change(record, desired or None)

    def export(self, env_id: str, output: Path) -> dict:
        view = self.inspect(env_id)
        output = Path(output)
        if not output.is_absolute():
            output = Path.cwd() / output
        self._write_export(output, view)
        return view

    def start(self, env_id: str) -> dict:
        env_id = validate_env_id(env_id)
        with self._lifecycle_lock(env_id):
            record = self.store.load(env_id)
            info = self._wait_while_stopping(record)
            if not info.running:
                runtime = record["runtime"]
                self.runtime.start(runtime["container_name"], project=runtime["project"],
                                   env_id=record["id"])
                kernel = self._optional_capture(record, ["uname", "-r"])
                if kernel:
                    record["observed"]["kernel"] = kernel.strip()
                record["runtime_state"] = "running"
                record["updated_at"] = self.clock()
                self.store.save(record, None)
        return self.inspect(env_id)

    def stop(self, env_id: str) -> dict:
        env_id = validate_env_id(env_id)
        with self._lifecycle_lock(env_id):
            record = self.store.load(env_id)
            info = self._wait_while_stopping(record)
            if info.running:
                runtime = record["runtime"]
                self.runtime.stop(runtime["container_name"], project=runtime["project"],
                                  env_id=record["id"])
            if info.running or record.get("runtime_state") != "stopped":
                record["runtime_state"] = "stopped"
                record["updated_at"] = self.clock()
                self.store.save(record, None)
        return self.inspect(env_id)

    def run(self, env_id: str, argv: list[str], *, workdir: str | None,
            env_keys: list[str]) -> int:
        if not argv or any(not isinstance(item, str) or item == "" for item in argv):
            raise MultilinuxError("argv must be a non-empty list of non-empty strings")
        if workdir is not None and (not workdir.startswith("/") or "\x00" in workdir):
            raise MultilinuxError("workdir must be an absolute path inside the environment")
        record = self.store.load(validate_env_id(env_id))
        info = self._require_owned(record)
        if not info.running:
            raise MultilinuxError(f"environment {record['id']} is {info.status}; start it before run")
        env = self._allowlist(env_keys)
        runtime = record["runtime"]
        result = self.runtime.exec(
            runtime["container_name"], argv, project=runtime["project"], env_id=record["id"],
            workdir=workdir, env=env, capture=False, timeout=None)
        return result.returncode

    def remove(self, env_id: str) -> None:
        record = self.store.load(validate_env_id(env_id))
        runtime = record["runtime"]
        name = runtime["container_name"]
        info = self.runtime.inspect(name)
        if info is None:
            self.store.delete(record["id"])
            return
        if not self._matches(info, record):
            raise MultilinuxError(
                f"refusing to remove {name}: it does not belong to environment {record['id']}")
        self.runtime.remove(name, project=runtime["project"], env_id=record["id"])
        self.store.delete(record["id"])

    def add_app(self, env_id: str, app_id: str, executable: str, argv: list[str], *,
                workdir: str | None = None, env_overlay: dict | None = None,
                as_host_user: bool = False, on_demand: bool = False,
                package_origin: dict | None = None) -> dict:
        app_id = validate_app_id(app_id)
        if self._launch_exists(app_id):
            raise MultilinuxError(f"application {app_id} already exists")
        record = self.store.load(validate_env_id(env_id))
        desktop = record.get("desktop")
        if package_origin and package_origin.get("target") != record["target"]:
            candidates = package_origin.get("candidates") or []
            if package_origin.get("target") is None and len(candidates) > 1:
                raise MultilinuxError(
                    "package format matches more than one target: " + ", ".join(candidates))
            raise MultilinuxError("package format does not match the application environment")
        if not desktop:
            raise MultilinuxError(
                f"environment {record['id']} has no desktop session; create it with --desktop")
        executable = self._absolute_executable(executable)
        if not argv or argv[0] != executable:
            raise MultilinuxError("argv must start with the executable")
        if any(not isinstance(item, str) or item == "" for item in argv):
            raise MultilinuxError("argv must be a non-empty list of non-empty strings")
        workdir = self._shared_workdir(record, workdir)
        env = dict(desktop["env"])
        for key, value in (env_overlay or {}).items():
            if not ENV_KEY.fullmatch(key) or not isinstance(value, str) or value == "" or "\n" in value:
                raise MultilinuxError(f"invalid application environment value: {key}")
            env[key] = value
        user = None
        if as_host_user:
            if record["runtime"].get("user_namespace") != "keep-id":
                raise MultilinuxError("--as-host-user requires an environment created with --userns keep-id")
            user = f"{self.uid}:{self.gid}"
            home = f"/home/multilinux-{self.uid}"
            try:
                username = pwd.getpwuid(self.uid).pw_name
            except KeyError:
                username = f"multilinux-{self.uid}"
            env.update(HOME=home, USER=username, LOGNAME=username)
        was_running = self._ensure_running(record["id"])
        try:
            if user is not None:
                # These are container directories, not the mounted session sockets.
                runtime_dir = desktop["env"].get("XDG_RUNTIME_DIR", f"/run/user/{self.uid}")
                directories = [env["HOME"], runtime_dir]
                if any(Path(directory).is_relative_to(item["container"])
                       for directory in directories for item in record["shared_mounts"]):
                    raise MultilinuxError("application home/runtime directory cannot be a shared mount")
                env["XDG_RUNTIME_DIR"] = runtime_dir
                prepared = self._exec_record(
                    self.store.load(record["id"]),
                    ["install", "-d", "-m", "700", "-o", str(self.uid), "-g", str(self.gid),
                     *directories], timeout=20, native=False)
                if prepared.returncode != 0:
                    raise MultilinuxError(f"cannot prepare application home/runtime: {prepared.stderr[-1500:]}")
            check = self._exec_record(self.store.load(record["id"]), ["test", "-x", executable],
                                      timeout=20, native=False)
            if check.returncode != 0:
                raise MultilinuxError(f"{executable} is not executable in {record['id']}")
        finally:
            if not was_running:
                with contextlib.suppress(Exception):
                    self.stop(record["id"])
        launch = {
            "schema_version": 1,
            "id": app_id,
            "environment_id": record["id"],
            "executable": executable,
            "argv": list(argv),
            "workdir": workdir,
            "env": env,
            "user": user,
            "on_demand": on_demand,
            "package_origin": package_origin,
            "mounts": list(desktop["mounts"]),
            "export": None,
            "created_at": self.clock(),
            "updated_at": self.clock(),
        }
        self.store.save_launch(launch)
        return launch

    def show_app(self, app_id: str) -> dict:
        return self.store.load_launch(validate_app_id(app_id))

    def open_app(self, app_id: str, extra: list[str] | None = None) -> ExecResult:
        """Open one recorded application.

        On-demand applications take the lease in a supervised worker instead of
        this short-lived CLI process, so killing the launcher cannot lose
        automatic stop ownership; manual (non on-demand) apps stay unmanaged.
        """
        launch = self.show_app(app_id)
        if launch.get('on_demand'):
            from multilinux.on_demand import supervised_open
            return supervised_open(self, app_id, extra or [])
        return self._open_app_unmanaged(app_id, extra)

    def _open_app_unmanaged(self, app_id: str, extra: list[str] | None = None) -> ExecResult:
        launch = self.show_app(app_id)
        argv = list(launch["argv"])
        for item in extra or []:
            if item == "":
                continue
            argv.append(item)
        return self.exec_app(app_id, argv, capture=False, timeout=None)

    def exec_app(self, app_id: str, argv: list[str], *, capture: bool = False,
                 timeout: int | None = None) -> ExecResult:
        if not argv or any(not isinstance(item, str) or item == "" for item in argv):
            raise MultilinuxError("argv must be a non-empty list of non-empty strings")
        launch = self.show_app(app_id)
        record = self.store.load(launch["environment_id"])
        self.start(record["id"])
        record = self.store.load(record["id"])
        runtime = record["runtime"]
        user_options = {"user": launch["user"]} if launch.get("user") else {}
        return self.runtime.exec(
            runtime["container_name"], argv, project=runtime["project"], env_id=record["id"],
            workdir=launch["workdir"], env=dict(launch["env"]), capture=capture, timeout=timeout,
            **user_options)

    def export_app(self, app_id: str, *, mime_type: str = "application/x-multilinux-note",
                   extension: str = "mlnote", menu_only: bool = False,
                   display_name: str | None = None, icon: str | None = None,
                   wm_class: str | None = None, desktop_alias: str | None = None) -> dict:
        if menu_only:
            return self._export_menu_app(app_id, display_name, icon, wm_class, desktop_alias)
        if not MIME_TYPE.fullmatch(mime_type):
            raise MultilinuxError(
                "file association type must look like application/x-multilinux-note")
        launch = self.show_app(app_id)
        if launch.get("export"):
            raise MultilinuxError(f"application {launch['id']} is already exported")
        xml = render_mime_xml(mime_type, extension)
        record = self.store.load(launch["environment_id"])
        project = record["runtime"]["project"]
        name = desktop_id(self.root, launch["id"])
        desktop_name = name + ".desktop"
        launcher = [sys.executable, str((self.root / "tools/ml.py").resolve()),
                    "app", "open", launch["id"], "--"]
        text = render_desktop_entry(
            launcher=launcher, app_id=launch["id"], environment_id=launch["environment_id"],
            project=project, mime_type=mime_type, name=f"Multilinux {launch['id']}")
        desktop_path = self.host_desktop.applications / desktop_name
        mime_path = self.host_desktop.mime_packages / f"{name}.xml"
        mimeapps = self.host_desktop.mimeapps
        previous = mimeapps.read_text(encoding="utf-8") if mimeapps.is_file() else None
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
        mime_path.parent.mkdir(parents=True, exist_ok=True)
        desktop_path.write_text(text, encoding="utf-8")
        mime_path.write_text(xml, encoding="utf-8")
        mimeapps.write_text(add_mime_association(previous, mime_type, desktop_name), encoding="utf-8")
        try:
            self.host_desktop.refresh(self.cache_runner)
        except Exception:
            desktop_path.unlink(missing_ok=True)
            mime_path.unlink(missing_ok=True)
            if previous is None:
                mimeapps.unlink(missing_ok=True)
            else:
                mimeapps.write_text(previous, encoding="utf-8")
            raise
        launch["export"] = {
            "desktop_id": name,
            "desktop_path": str(desktop_path),
            "mime_package": str(mime_path),
            "mime_type": mime_type,
            "extension": extension,
            "mimeapps_existed": previous is not None,
            "mimeapps_previous": previous,
        }
        launch["updated_at"] = self.clock()
        self.store.save_launch(launch)
        return launch

    def _export_menu_app(self, app_id, display_name, icon, wm_class, desktop_alias):
        launch = self.show_app(app_id)
        if launch.get('export'):
            raise MultilinuxError(f"application {app_id} is already exported")
        name = validate_app_id(desktop_alias) if desktop_alias else desktop_id(self.root, app_id)
        path = self.host_desktop.applications / (name + '.desktop')
        if path.exists():
            raise MultilinuxError(f'refusing to replace an existing desktop entry: {path}')
        record = self.store.load(launch['environment_id'])
        text = render_desktop_entry(
            launcher=[sys.executable, str((self.root / 'tools/ml.py').resolve()), 'app', 'open', app_id, '--'],
            app_id=app_id, environment_id=record['id'], project=record['runtime']['project'],
            mime_type=None, name=display_name or app_id, icon=icon, wm_class=wm_class)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        try:
            self.host_desktop.refresh_applications(self.cache_runner)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        launch['export'] = {'mode': 'menu', 'desktop_id': name, 'desktop_path': str(path)}
        launch['updated_at'] = self.clock()
        self.store.save_launch(launch)
        return launch

    def unexport_app(self, app_id: str) -> dict:
        launch = self.show_app(app_id)
        exported = launch.get("export")
        if not exported:
            return launch
        desktop_path = Path(exported["desktop_path"])
        if exported.get('mode') == 'menu':
            self._remove_our_desktop_file(desktop_path, launch)
            self.host_desktop.refresh_applications(self.cache_runner)
            launch['export'] = None
            launch['updated_at'] = self.clock()
            self.store.save_launch(launch)
            return launch
        mime_path = Path(exported["mime_package"])
        self._remove_our_desktop_file(desktop_path, launch)
        mime_path.unlink(missing_ok=True)
        mimeapps = self.host_desktop.mimeapps
        if mimeapps.is_file():
            updated = remove_mime_association(
                mimeapps.read_text(encoding="utf-8"), exported["mime_type"],
                Path(exported["desktop_path"]).name)
            if updated:
                mimeapps.write_text(updated, encoding="utf-8")
            elif not exported.get("mimeapps_existed"):
                mimeapps.unlink(missing_ok=True)
            else:
                mimeapps.write_text(updated, encoding="utf-8")
        with contextlib.suppress(MultilinuxError):
            self.host_desktop.refresh(self.cache_runner)
        launch["export"] = None
        launch["updated_at"] = self.clock()
        self.store.save_launch(launch)
        return launch

    def remove_app(self, app_id: str) -> None:
        if self._launch_exists(validate_app_id(app_id)):
            self.unexport_app(app_id)
        self.store.delete_launch(app_id)

    def query_package(self, env_id: str, name: str) -> dict:
        name = validate_package_name(name)
        record = self.store.load(validate_env_id(env_id))
        driver = driver_for(record)
        was_running = self._ensure_running(record["id"])
        try:
            result = self._exec_record(
                self.store.load(record["id"]), driver.query(name),
                timeout=30, native=True)
            detail = (result.stdout or result.stderr).strip()
            return {
                "environment_id": record["id"],
                "package": name,
                "present": result.returncode == 0,
                "exit_code": result.returncode,
                "detail": detail[-1500:],
            }
        finally:
            if not was_running:
                with contextlib.suppress(Exception):
                    self.stop(record["id"])

    def install_packages(self, env_id: str, packages: list[str], *, use_argv=None) -> dict:
        return self._package_transaction(env_id, "install", packages, use_argv=use_argv)

    def update_packages(self, env_id: str, *, use_argv=None) -> dict:
        return self._package_transaction(env_id, "update", [], use_argv=use_argv)

    def remove_packages(self, env_id: str, packages: list[str], *, use_argv=None) -> dict:
        return self._package_transaction(env_id, "remove", packages, use_argv=use_argv)

    def install_local(self, env_id: str, host_file: str, *, package_name: str | None = None,
                      use_argv=None) -> dict:
        names = [package_name] if package_name else []
        return self._package_transaction(
            env_id, "install-local", names, use_argv=use_argv, local_file=host_file)

    def transactions_for(self, env_id: str) -> list[dict]:
        return self.store.transactions_for(validate_env_id(env_id))

    def show_transaction(self, txn_id: str) -> dict:
        if not re.fullmatch(r"txn[0-9a-f]{12}", txn_id):
            raise MultilinuxError("transaction id must look like txn followed by 12 hex digits")
        return self.store.load_transaction(txn_id)

    def _package_transaction(self, env_id: str, operation: str, packages: list[str], *,
                             use_argv=None, local_file: str | None = None) -> dict:
        record = self.store.load(validate_env_id(env_id))
        decision = plan_change(record, operation="package-change")
        if decision["action"] != "upgrade":
            raise MultilinuxError(decision.get("reason") or "this environment cannot be upgraded in place")
        names = [validate_package_name(name) for name in packages]
        use_argv = validate_use_argv(use_argv)
        if operation in {"install", "remove"} and not names:
            raise MultilinuxError(f"{operation} requires at least one package name")
        if operation == "update" and names:
            raise MultilinuxError("update upgrades the whole environment and does not take package names")
        network = record["runtime"].get("network", "none")
        if operation in {"install", "update", "install-local"} and network != "pasta":
            raise MultilinuxError(
                f"environment {record['id']} was created with network {network}; "
                "install and update need --network pasta at create time")
        local_path = None
        if operation == "install-local":
            if not local_file:
                raise MultilinuxError("install-local requires a package file")
            local_path = local_package_path(record["target"], local_file)

        txn = {
            "schema_version": 1,
            "id": "txn" + uuid.uuid4().hex[:12],
            "environment_id": record["id"],
            "operation": operation,
            "packages": names,
            "use_command": use_argv,
            "local_file": None if local_path is None else str(local_path),
            "revision_before": {
                "writable_revision": record["writable_revision"],
                "content_revision": int(record.get("content_revision") or 1),
                "package_inventory_sha256": record["package_inventory_sha256"],
            },
            "native": [],
            "native_error": None,
            # Hooks run after the package files are written. Their failures do
            # not uninstall the package, so they are recorded beside the phase
            # and summarized by the top-level verdict.
            "maintenance": {
                "status": "ok",
                "items": [],
                "system_integration": "unverified",
                "full_init": False,
            },
            "verdict": None,
            "verification": None,
            "host_entries": None,
            "host_packages": None,
            "other_environments": None,
            "revision_after": None,
            "phase": "failed",
            "failure_phase": None,
            "started_at": self.clock(),
            "finished_at": None,
        }
        inventory_text = None
        was_running = False
        other_before = None
        host_before = None
        try:
            other_before = self._other_fingerprints(record["id"])
            host_before = self.host_observer.snapshot()
            was_running = self._ensure_running(record["id"])
            record = self.store.load(record["id"])
            steps = self._native_steps(record, operation, names, local_path)
            self._run_native(record, txn, steps)
            if _keyring_ready(txn["native"]):
                self._mark_keyring(record["id"])
                record = self.store.load(record["id"])
            inventory_text = self._read_inventory_record(record)
            txn["verification"] = self._verify(record, operation, names, use_argv, inventory_text, txn)
            if txn["failure_phase"] is None and not txn["verification"]["ok"]:
                txn["failure_phase"] = "verification"
        except Exception as exc:
            if txn["failure_phase"] is None:
                txn["failure_phase"] = "runtime"
                txn["native_error"] = str(exc)[-1500:]
        finally:
            if not was_running:
                with contextlib.suppress(Exception):
                    self.stop(record["id"])
            record = self.store.load(record["id"])
            if inventory_text is not None or _syu_succeeded(txn["native"]):
                record = self._commit_inventory(record, inventory_text, txn)
            self._finish_isolation(txn, other_before, host_before, record["id"])
            if txn.get("verification") and not txn["verification"].get("ok") and txn["failure_phase"] is None:
                txn["failure_phase"] = "verification"
            if txn.get("verification") is None and txn["failure_phase"] is None:
                txn["failure_phase"] = "runtime"
            if not txn["native"] and txn["failure_phase"] is None:
                txn["failure_phase"] = "runtime"
                txn["native_error"] = "no native package command was run"
            txn["revision_after"] = {
                "writable_revision": record["writable_revision"],
                "content_revision": int(record.get("content_revision") or 1),
                "package_inventory_sha256": record["package_inventory_sha256"],
            }
            txn["phase"] = "completed" if txn["failure_phase"] is None else "failed"
            maintenance = txn.get("maintenance") or {}
            maintenance["status"] = "warnings" if maintenance.get("items") else "ok"
            maintenance["system_integration"] = "unverified"
            maintenance["full_init"] = False
            txn["maintenance"] = maintenance
            txn["verdict"] = transaction_verdict(txn["phase"], maintenance)
            txn["finished_at"] = self.clock()
            self.store.save_transaction(txn)
            self.log(f"{txn['environment_id']}: {txn['operation']} {txn['phase']} {txn['id']}")
        return txn

    def _native_steps(self, record: dict, operation: str, names: list[str],
                      local_path: Path | None) -> list[tuple[list[str], int]]:
        driver = driver_for(record)
        steps: list[tuple[list[str], int]] = []
        if driver.needs_keyring and operation in {"install", "update", "install-local"}:
            initialized = (record.get("arch") or {}).get("keyring_initialized_at")
            if not initialized:
                steps.extend(driver.bootstrap(populate=self._arch_needs_populate(record)))
        if operation == "install":
            steps.extend(driver.install(names))
        elif operation == "update":
            steps.extend(driver.update())
        elif operation == "remove":
            steps.extend(driver.remove(names))
        elif operation == "install-local":
            container_path = self._stage_local(record, local_path)
            steps.extend(driver.local(container_path))
        return steps

    def _arch_needs_populate(self, record: dict) -> bool:
        result = self._exec_record(
            record, ["pacman-key", "--list-keys"], timeout=TIMEOUT_PACMAN_KEY, native=True)
        if result.returncode != 0:
            return True
        return not result.stdout.strip()

    def _stage_local(self, record: dict, local_path: Path) -> str:
        container_path = f"{INCOMING_DIR}/{local_path.name}"
        self._exec_record(record, ["mkdir", "-p", INCOMING_DIR], timeout=30, native=False)
        runtime = record["runtime"]
        self.runtime.copy_in(
            runtime["container_name"], str(local_path), container_path,
            project=runtime["project"], env_id=record["id"])
        return container_path

    def _run_native(self, record: dict, txn: dict, steps: list[tuple[list[str], int]]) -> None:
        for argv, timeout in steps:
            self.log(f"{record['id']}: {' '.join(argv)}")
            result = self._exec_record(record, argv, timeout=timeout, native=True)
            txn["native"].append({
                "argv": list(argv),
                "exit_code": result.returncode,
                "stdout_tail": _tail(result.stdout or ""),
                "stderr_tail": _tail(result.stderr or ""),
            })
            # The saved tails are for reading; the hook scan uses the complete
            # output because a failure line can sit in the leading part.
            findings = maintenance_findings(
                record["target"], result.stdout or "", result.stderr or "")
            if findings:
                maintenance = txn["maintenance"]
                maintenance["items"] = merge_maintenance_findings(
                    maintenance.get("items"), findings)
                maintenance["status"] = "warnings"
            if result.returncode != 0:
                txn["native_error"] = explain_native_error(
                    (result.stderr or "") + "\n" + (result.stdout or ""))
                txn["failure_phase"] = "native-command"
                return

    def _verify(self, record: dict, operation: str, names: list[str], use_argv: list[str],
                inventory_text: str, txn: dict) -> dict:
        present = {name: inventory_has(inventory_text, name) for name in names}
        problems = []
        if txn["failure_phase"] == "native-command":
            return {
                "ok": False,
                "packages": present,
                "command": None,
                "command_exit_code": None,
                "stdout_preview": None,
                "stdout_sha256": None,
                "problems": ["native command failed"],
            }
        for name, found in present.items():
            if operation in {"install", "install-local"} and not found:
                problems.append(f"{name} is not in the inventory")
            if operation == "remove" and found:
                problems.append(f"{name} is still in the inventory")
        command = None
        code = None
        preview = None
        digest = None
        if use_argv:
            result = self._exec_record(record, use_argv, timeout=60, native=False)
            command = use_argv
            code = result.returncode
            preview = (result.stdout or "")[:500]
            digest = hashlib.sha256((result.stdout or "").encode()).hexdigest()
            if operation == "remove":
                if code == 0:
                    problems.append("use command still exited 0 after remove")
            elif code != 0:
                problems.append(f"use command exited {code}")
        return {
            "ok": not problems,
            "packages": present,
            "command": command,
            "command_exit_code": code,
            "stdout_preview": preview,
            "stdout_sha256": digest,
            "problems": problems,
        }

    def _commit_inventory(self, record: dict, inventory_text: str | None, txn: dict) -> dict:
        record = self.store.load(record["id"])
        stamp = self.clock()
        inventory_to_save = None
        changed = False
        if inventory_text is not None:
            new_hash = hashlib.sha256(inventory_text.encode()).hexdigest()
            if new_hash != record["package_inventory_sha256"]:
                record["content_revision"] = int(record.get("content_revision") or 1) + 1
                record["package_inventory_sha256"] = new_hash
                record["package_inventory_count"] = inventory_text.count("\n")
                record["updated_at"] = stamp
                inventory_to_save = inventory_text
                changed = True
        if (_syu_succeeded(txn["native"]) and driver_for(record["target"]).tracks_package_set
                and record.get("arch")):
            record["arch"]["repository_updated_at"] = stamp
            record["arch"]["package_set_sha256"] = record["package_inventory_sha256"]
            record["updated_at"] = stamp
            changed = True
        if changed:
            self.store.save(record, inventory_to_save)
            record = self.store.load(record["id"])
        return record

    def _finish_isolation(self, txn: dict, other_before, host_before, env_id: str) -> None:
        try:
            other_after = self._other_fingerprints(env_id)
            other_error = None
        except Exception as exc:
            other_after = {}
            other_error = str(exc)[-500:]
        try:
            host_after = self.host_observer.snapshot()
        except Exception as exc:
            host_after = {
                "packages": {
                    "available": False, "tool": None, "sha256": None, "count": None,
                    "error": str(exc)[-500:],
                },
                "entries": {"directory": "", "names": []},
            }
        if other_before is None or other_error:
            txn["other_environments"] = {
                "_error": other_error or "peer snapshot failed before the native command",
            }
        else:
            txn["other_environments"] = _compare_others(other_before, other_after)
        before_packages = (host_before or {}).get("packages") or {
            "available": False, "tool": None, "sha256": None, "count": None,
            "error": "host snapshot failed before the native command",
        }
        after_packages = host_after.get("packages") or {}
        txn["host_packages"] = {
            "before": _package_view(before_packages),
            "after": _package_view(after_packages),
        }
        before_names = ((host_before or {}).get("entries") or {}).get("names") or []
        after_names = (host_after.get("entries") or {}).get("names") or []
        txn["host_entries"] = entry_changes(list(before_names), list(after_names))
        problems = _isolation_problems(txn)
        if not problems:
            return
        verification = txn.get("verification") or {
            "ok": False, "packages": {}, "problems": [], "command": None,
            "command_exit_code": None, "stdout_preview": None, "stdout_sha256": None,
        }
        verification["ok"] = False
        verification["isolation"] = problems
        txn["verification"] = verification
        if txn["failure_phase"] is None:
            txn["failure_phase"] = "verification"

    def _other_fingerprints(self, env_id: str) -> dict:
        found = {}
        for other in self.store.all():
            if other["id"] == env_id:
                continue
            probed, error = self._probe_inventory(other)
            found[other["id"]] = {
                "stored": other["package_inventory_sha256"],
                "probed": probed,
                "error": error,
            }
        return found

    def _probe_inventory(self, record: dict) -> tuple[str | None, str | None]:
        """Read one peer's inventory. A stopped peer is stopped again afterwards."""
        try:
            info = self._require_owned(record)
        except MultilinuxError as exc:
            return None, str(exc)
        started_here = False
        try:
            if not info.running:
                self.start(record["id"])
                started_here = True
            text = self._read_inventory_record(self.store.load(record["id"]))
            return hashlib.sha256(text.encode()).hexdigest(), None
        except Exception as exc:
            return None, str(exc)[-500:]
        finally:
            if started_here:
                with contextlib.suppress(Exception):
                    self.stop(record["id"])

    @contextlib.contextmanager
    def _lifecycle_lock(self, env_id: str):
        """Serialize one environment's start/stop, also across processes.

        LaunchGroup's lease lock protects the lease file; this separate lock
        protects the container itself, so two launchers cannot start the same
        environment concurrently. It is always taken after the lease lock and
        never before, so the two locks cannot deadlock.
        """
        directory = self.root / ".lab/leases"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{env_id}.lifecycle.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _wait_while_stopping(self, record: dict) -> ContainerState:
        """Wait for an in-flight podman stop; a stopping container is not stopped.

        Starting podman while it still stops fails, and releasing a lease on a
        stopping container can start a second one too early. The wait is bounded:
        after the deadline the caller refuses to race rather than double-start.
        """
        info = self._require_owned(record)
        deadline = time.monotonic() + LIFECYCLE_SETTLE_SECONDS
        while info.status == "stopping":
            if time.monotonic() >= deadline:
                raise MultilinuxError(
                    f"environment {record['id']} is still stopping; refusing to race podman")
            time.sleep(LIFECYCLE_SETTLE_POLL_SECONDS)
            info = self._require_owned(record)
        return info

    def _ensure_running(self, env_id: str) -> bool:
        record = self.store.load(env_id)
        info = self._require_owned(record)
        if info.running:
            return True
        self.start(env_id)
        return False

    def _exec_record(self, record: dict, argv: list[str], *, timeout: int | None, native: bool):
        runtime = record["runtime"]
        return self.runtime.exec(
            runtime["container_name"], argv, project=runtime["project"], env_id=record["id"],
            workdir=None, env=driver_for(record).native_env() if native else {},
            capture=True, timeout=timeout)

    def _read_inventory_record(self, record: dict) -> str:
        spec = CreateSpec(
            name=record["runtime"]["container_name"],
            env_id=record["id"],
            project=record["runtime"]["project"],
            image=record["base_image"]["locked_reference"],
            mounts=(),
            network=record["runtime"].get("network", "none"),
        )
        return self._read_inventory(spec, record["target"])

    def _mark_keyring(self, env_id: str) -> None:
        record = self.store.load(env_id)
        arch = record.get("arch")
        if not isinstance(arch, dict):
            return
        arch["keyring_initialized_at"] = self.clock()
        record["updated_at"] = self.clock()
        self.store.save(record, None)

    def _record(self, spec: CreateSpec, target_name: str, target: dict, image: str,
                identity: dict, inventory: str, info: ContainerState,
                desktop_session: dict | None) -> dict:
        stamp = self.clock()
        inventory_hash = hashlib.sha256(inventory.encode()).hexdigest()
        lock_item = self.lock["targets"][target_name]
        version_track = ("rolling" if target["update_policy"] == "full-system-upgrade"
                         else target["expected_version_id"])
        arch = None
        if target_name == "arch":
            arch = {
                "image_version_label": lock_item.get("image_version_label"),
                "os_release_version_id": identity["version_id"] or None,
                "os_release_build_id": identity["build_id"] or None,
                "repository_updated_at": None,
                "package_set_sha256": inventory_hash,
                "keyring_initialized_at": None,
            }
        return {
            "schema_version": 1,
            "id": spec.env_id,
            "target": target_name,
            "distribution_id": identity["id"],
            "version_track": version_track,
            "update_policy": target["update_policy"],
            "architecture": "amd64",
            "base_image": {
                "source": lock_item["source"],
                "manifest_digest": lock_item["manifest_digest"],
                "locked_reference": image,
                "image_version_label": lock_item.get("image_version_label"),
            },
            "writable_revision": info.container_id,
            "content_revision": 1,
            "runtime_state": "stopped",
            "package_inventory_sha256": inventory_hash,
            "package_inventory_count": inventory.count("\n"),
            "package_inventory_path": "",
            "shared_mounts": [
                {"host": host, "container": container, "options": "z"}
                for host, container in spec.mounts
            ],
            "desktop": desktop_session,
            "created_at": stamp,
            "updated_at": stamp,
            "arch": arch,
            "runtime": {
                "backend": "podman",
                "container_name": spec.name,
                "container_id": info.container_id,
                "project": spec.project,
                "network": spec.network,
                "selinux_label": spec.selinux_label,
                "full_init": False,
                "user_namespace": spec.user_namespace,
                "devices": list(spec.devices),
                "ipc": spec.ipc,
                "rootless": True,
            },
            "observed": {
                "os_release_id": identity["id"],
                "os_release_version_id": identity["version_id"] or None,
                "os_release_build_id": identity["build_id"] or None,
                "os_release_pretty_name": identity["pretty_name"] or None,
                "kernel": identity["kernel"],
                "machine": identity["machine"],
            },
            "system": system_contract(target_name, target),
            "created_by_uid": self.uid,
            "created_by_gid": self.gid,
            "scope": (
                "Ordinary application environment with host session sockets mounted. "
                "Launch records are stored separately. Full init is a later stage."
                if desktop_session else
                "Ordinary application environment. Native package transactions are recorded "
                "separately. Host desktop sockets are not mounted."
            ),
        }

    def _live(self, record: dict) -> dict:
        view = dict(record)
        info = self.runtime.inspect(record["runtime"]["container_name"])
        if info is None:
            view["runtime_state"] = "missing"
            view["resources"] = None
        elif not self._matches(info, record):
            view["runtime_state"] = "foreign"
            view["resources"] = None
        else:
            view["runtime_state"] = "running" if info.running else "stopped"
            view["writable_revision"] = info.container_id
            view["resources"] = {
                "status": info.status,
                "pid": info.pid,
                "conmon_pid": info.conmon_pid,
                "started_at": info.started_at,
                "finished_at": info.finished_at,
                "oom_killed": info.oom_killed,
                "exit_code": info.exit_code,
            }
        view["host_kernel"] = self.host_kernel
        view["kernel_matches_current_host"] = record["observed"]["kernel"] == self.host_kernel
        return view

    def _read_identity(self, spec: CreateSpec) -> dict:
        release = parse_os_release(self._capture(spec, ["cat", "/etc/os-release"]))
        return {
            "id": release.get("ID", ""),
            "version_id": release.get("VERSION_ID", ""),
            "build_id": release.get("BUILD_ID", ""),
            "pretty_name": release.get("PRETTY_NAME", ""),
            "kernel": self._capture(spec, ["uname", "-r"]).strip(),
            "machine": self._capture(spec, ["uname", "-m"]).strip(),
        }

    def _launch_exists(self, app_id: str) -> bool:
        return self.store.launch_path(app_id).is_file()

    def _absolute_executable(self, executable: str) -> str:
        if not isinstance(executable, str) or not executable.startswith("/") or "\n" in executable or "\x00" in executable:
            raise MultilinuxError("executable must be an absolute path inside the environment")
        return executable

    def _shared_workdir(self, record: dict, workdir: str | None) -> str | None:
        if workdir is None:
            return None
        if not workdir.startswith("/") or "\n" in workdir or "\x00" in workdir:
            raise MultilinuxError("workdir must be an absolute path")
        shares = [item["host"] for item in record.get("shared_mounts") or []]
        if not any(workdir == host or workdir.startswith(host + "/") for host in shares):
            raise MultilinuxError("workdir must stay inside a shared mount")
        return workdir

    def _remove_our_desktop_file(self, path: Path, launch: dict) -> None:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        if f"X-Multilinux-App={launch['id']}" not in text.splitlines():
            raise MultilinuxError(f"refusing to remove {path}: it is not application {launch['id']}")
        path.unlink()

    def _validate_identity(self, target_name: str, target: dict, identity: dict) -> None:
        if identity["id"] != target["expected_id"]:
            raise MultilinuxError(
                f"{target_name} reported distribution {identity['id']!r}, "
                f"expected {target['expected_id']!r}")
        expected_version = target.get("expected_version_id")
        if expected_version is not None and identity["version_id"] != expected_version:
            raise MultilinuxError(
                f"{target_name} reported version {identity['version_id']!r}, "
                f"expected {expected_version!r}")
        if identity["machine"] != "x86_64":
            raise MultilinuxError(f"{target_name} reported architecture {identity['machine']!r}")
        if identity["kernel"] != self.host_kernel:
            raise MultilinuxError(
                f"{target_name} kernel {identity['kernel']!r} does not match "
                f"host kernel {self.host_kernel!r}")

    def _read_inventory(self, spec: CreateSpec, target_name: str) -> str:
        argv = inventory_argv(target_name)
        return normalize_inventory(self._capture(spec, argv, timeout=60))

    def _capture(self, spec: CreateSpec, argv: list[str], timeout: int = 30) -> str:
        result = self.runtime.exec(
            spec.name, argv, project=spec.project, env_id=spec.env_id,
            workdir=None, env={}, capture=True, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise MultilinuxError(f"{' '.join(argv)} failed in {spec.env_id}: {detail[-1500:]}")
        return result.stdout

    def _optional_capture(self, record: dict, argv: list[str]) -> str:
        runtime = record["runtime"]
        result = self.runtime.exec(
            runtime["container_name"], argv, project=runtime["project"], env_id=record["id"],
            workdir=None, env={}, capture=True, timeout=20)
        if result.returncode != 0:
            return ""
        return result.stdout

    def _allowlist(self, keys: list[str]) -> dict[str, str]:
        env = {}
        for key in keys:
            if not ENV_KEY.fullmatch(key):
                raise MultilinuxError(f"invalid environment variable name: {key}")
            if key not in os.environ:
                raise MultilinuxError(f"allowlisted variable is not set: {key}")
            env[key] = os.environ[key]
        return env

    def _shares(self, shares: list[str]) -> list[tuple[str, str]]:
        root = self.root.resolve()
        state = (root / ".lab").resolve()
        mounts = []
        seen = set()
        for raw in shares:
            path = Path(raw)
            if not path.is_absolute():
                raise MultilinuxError(f"shared path must be absolute: {raw}")
            resolved = path.resolve()
            if not resolved.is_dir():
                raise MultilinuxError(f"shared path is not a directory: {resolved}")
            container = resolved.as_posix()
            if ":" in container or "\n" in container or "\x00" in container:
                raise MultilinuxError(
                    f"shared path cannot contain ':' or a newline; podman --volume uses ':' as a separator: {container}")
            if _hides_system_tree(container):
                raise MultilinuxError(f"refusing to mount over a system directory: {container}")
            if resolved == root or root.is_relative_to(resolved):
                raise MultilinuxError(f"refusing to share a parent of the project: {resolved}")
            if resolved == state or resolved.is_relative_to(state) or state.is_relative_to(resolved):
                raise MultilinuxError(f"refusing to share laboratory state: {resolved}")
            if container in seen:
                raise MultilinuxError(f"duplicate shared path: {container}")
            seen.add(container)
            mounts.append((container, container))
        return mounts

    def _target(self, name: str) -> dict:
        try:
            return self.config["targets"][name]
        except KeyError as exc:
            raise MultilinuxError(f"unknown target {name}") from exc

    def _plan_share(self, raw: str) -> str:
        path = Path(raw)
        if not path.is_absolute():
            raise MultilinuxError(f"shared path must be absolute: {raw}")
        return path.resolve().as_posix()

    def _lock_digest(self, target_name: str) -> str:
        return self._locked_reference(target_name).rsplit("@", 1)[1]

    def _locked_reference(self, target_name: str) -> str:
        try:
            item = self.lock["targets"][target_name]
        except KeyError as exc:
            raise MultilinuxError(f"image lock has no entry for {target_name}") from exc
        digest = item.get("manifest_digest", "")
        reference = item.get("locked_reference", "")
        if not DIGEST.fullmatch(digest) or not reference.endswith("@" + digest):
            raise MultilinuxError(f"refusing to create {target_name} from an unlocked reference")
        return reference

    def _require_owned(self, record: dict) -> ContainerState:
        info = self.runtime.inspect(record["runtime"]["container_name"])
        if info is None:
            raise MultilinuxError(f"environment {record['id']} has no container")
        if not self._matches(info, record):
            raise MultilinuxError(
                f"refusing to operate on {record['runtime']['container_name']}: "
                f"it does not belong to environment {record['id']}")
        return info

    def _require_owned_record(self, name: str, project: str, env_id: str) -> ContainerState:
        info = self.runtime.inspect(name)
        if info is None or info.project != project or info.env_id != env_id or info.role != ROLE_ENVIRONMENT:
            raise MultilinuxError(f"created container {name} failed the ownership check")
        if not info.container_id:
            raise MultilinuxError(f"created container {name} has no id")
        return info

    def _matches(self, info: ContainerState, record: dict) -> bool:
        runtime = record["runtime"]
        return (info.project == runtime["project"] and info.env_id == record["id"]
                and info.role == ROLE_ENVIRONMENT and info.name == runtime["container_name"])

    def _write_export(self, output: Path, view: dict) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".ml-export-", dir=output.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(view, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
