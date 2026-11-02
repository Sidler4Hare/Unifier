"""Rootless Podman adapter for project-owned environments.

Every start, exec, stop and remove addresses one container by exact name and
refuses the call unless that container carries this project's environment
labels. There is no name-prefix or "all containers" operation.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess

from multilinux.errors import MultilinuxError


LABEL_PROJECT = "multilinux.project"
LABEL_ENV = "multilinux.env"
LABEL_ROLE = "multilinux.role"
ROLE_ENVIRONMENT = "environment"

# Exec-form entrypoint is not resolved through PATH.
SLEEPER = "/usr/bin/sleep"


@dataclass(frozen=True)
class CreateSpec:
    name: str
    env_id: str
    project: str
    image: str
    mounts: tuple[tuple[str, str], ...]
    network: str = "none"
    # Session sockets are mounted without a relabel. :z would change the
    # compositor's own label on the host.
    session_mounts: tuple[tuple[str, str], ...] = ()
    selinux_label: str = "default"
    user_namespace: str = "rootless-default"
    devices: tuple[str, ...] = ()
    ipc: str = "private"


@dataclass(frozen=True)
class ContainerState:
    name: str
    container_id: str
    running: bool
    status: str
    project: str
    env_id: str
    role: str
    image: str
    pid: int | None
    started_at: str | None
    finished_at: str | None
    oom_killed: bool | None
    exit_code: int | None
    conmon_pid: int | None = None


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


def project_token(project_root: Path) -> str:
    import hashlib
    return hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()[:8]


def container_name(project_root: Path, env_id: str) -> str:
    return f"ml-{project_token(project_root)}-{env_id}"


def project_label(project_root: Path) -> str:
    return str(project_root.resolve())


def creation_argv(spec: CreateSpec) -> list[str]:
    """Create a stopped, writable environment.

    Rootless Podman's default user namespace maps container root to the host
    user, so files this environment creates on a shared mount are owned by
    that user. Optional keep-id maps the host UID/GID to themselves for GUI
    applications. Its package commands still run as container root, which
    maps to a subordinate host UID.

    The root is writable because native package transactions change it.
    Network is chosen here: none has no repository access, pasta does.
    """
    if "@sha256:" not in spec.image:
        raise MultilinuxError(f"refusing to create from an unlocked image reference: {spec.image}")
    if spec.network not in {"none", "pasta"}:
        raise MultilinuxError("environment network must be none or pasta")
    if spec.selinux_label not in {"default", "disable"}:
        raise MultilinuxError("selinux label must be default or disable")
    if spec.ipc not in {"private", "host"}:
        raise MultilinuxError("ipc mode must be private or host")
    if spec.user_namespace not in {"rootless-default", "keep-id"}:
        raise MultilinuxError("user namespace must be rootless-default or keep-id")
    argv = [
        "podman", "create",
        "--name", spec.name,
        "--label", f"{LABEL_PROJECT}={spec.project}",
        "--label", f"{LABEL_ENV}={spec.env_id}",
        "--label", f"{LABEL_ROLE}={ROLE_ENVIRONMENT}",
        "--pull=never",
        "--network=" + spec.network,
        "--http-proxy=false",
        "--restart=no",
        "--stop-timeout=8",
        "--entrypoint", SLEEPER,
    ]
    if spec.ipc == "host":
        argv.append("--ipc=host")
    if spec.user_namespace == "keep-id":
        argv.extend(["--userns=keep-id", "--user", "0", "--passwd-entry",
                     "$USERNAME:*:$UID:$GID:$NAME:/home/multilinux-$UID:/bin/sh"])
    for device in spec.devices:
        if not re.fullmatch(r"(/dev/video[0-9]+|/dev/dri/(card|renderD)[0-9]+)", device):
            raise MultilinuxError("desktop device must be an explicit /dev/videoN camera or /dev/dri device")
        argv.extend(["--device", f"{device}:{device}:rw"])
    for host, container in spec.mounts:
        argv.extend(["--volume", f"{host}:{container}:z"])
    for host, container in spec.session_mounts:
        if ":" in host or ":" in container:
            raise MultilinuxError("session mount paths cannot contain ':'")
        argv.extend(["--volume", f"{host}:{container}"])
    if spec.selinux_label == "disable":
        argv.extend(["--security-opt", "label=disable"])
    argv.extend([spec.image, "infinity"])
    return argv


def exec_argv(name: str, argv: list[str], *, workdir: str | None, env: dict[str, str],
              user: str | None = None) -> list[str]:
    """Exec argv directly. Only the allowlisted variables are passed through."""
    if not argv:
        raise MultilinuxError("argv is empty")
    command = ["podman", "exec"]
    if user is not None:
        command.extend(["--user", user])
    if workdir is not None:
        command.extend(["--workdir", workdir])
    for key, value in env.items():
        command.extend(["--env", f"{key}={value}"])
    command.append(name)
    command.extend(argv)
    return command


def container_state_from_inspect(name: str, data: dict) -> ContainerState:
    labels = (data.get("Config") or {}).get("Labels") or {}
    state = data.get("State") or {}
    return ContainerState(
        name=name,
        container_id=data.get("Id") or "",
        running=bool(state.get("Running")),
        status=state.get("Status") or "unknown",
        project=labels.get(LABEL_PROJECT, ""),
        env_id=labels.get(LABEL_ENV, ""),
        role=labels.get(LABEL_ROLE, ""),
        image=data.get("ImageName") or data.get("Image") or "",
        pid=state.get("Pid"),
        started_at=state.get("StartedAt"),
        finished_at=state.get("FinishedAt"),
        oom_killed=state.get("OOMKilled"),
        exit_code=state.get("ExitCode"),
        conmon_pid=state.get("ConmonPid") or None,
    )


class PodmanRuntime:
    def __init__(self, runner=None, *, check_rootless=True):
        self.runner = runner or _run_podman
        self.check_rootless = check_rootless
        self._rootless_ok = False

    def create(self, spec: CreateSpec) -> str:
        self._ensure_rootless()
        if self.inspect(spec.name) is not None:
            raise MultilinuxError(f"container {spec.name} already exists")
        result = self.runner(creation_argv(spec), capture=True, timeout=60)
        if result.returncode:
            raise MultilinuxError(f"podman create failed: {(result.stderr or '')[-2000:]}")
        container_id = (result.stdout or "").strip()
        if not container_id:
            raise MultilinuxError("podman create did not return a container id")
        return container_id

    def inspect(self, name: str) -> ContainerState | None:
        self._ensure_rootless()
        result = self.runner(["podman", "inspect", "--format", "json", name],
                             capture=True, timeout=20)
        if result.returncode:
            detail = (result.stderr or "") + (result.stdout or "")
            if "no such" in detail.lower():
                return None
            raise MultilinuxError(f"podman inspect failed: {detail[-1500:]}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MultilinuxError(f"podman inspect returned invalid JSON: {exc}") from exc
        if not payload:
            return None
        return container_state_from_inspect(name, payload[0])

    def start(self, name: str, *, project: str, env_id: str) -> None:
        self._require_owned(name, project, env_id)
        self._check(["podman", "start", name], timeout=40, action="start")

    def stop(self, name: str, *, project: str, env_id: str) -> None:
        self._require_owned(name, project, env_id)
        self._check(["podman", "stop", "--time", "8", name], timeout=30, action="stop")

    def remove(self, name: str, *, project: str, env_id: str) -> None:
        self._require_owned(name, project, env_id)
        self._check(["podman", "rm", "--force", name], timeout=40, action="remove")

    def copy_in(self, name: str, host_path: str, container_path: str, *, project: str, env_id: str) -> None:
        self._require_owned(name, project, env_id)
        self._check(["podman", "cp", "--pause=false", host_path, f"{name}:{container_path}"],
                    timeout=60, action="cp")

    def container_size(self, name: str, *, project: str, env_id: str) -> dict:
        """Writable layer and whole-root size as reported by Podman.

        SizeRootFs includes shared lower layers, so it is not exclusive space.
        """
        self._require_owned(name, project, env_id)
        result = self.runner(
            ["podman", "container", "inspect", "--size", "--format", "{{json .}}", name],
            capture=True, timeout=120)
        if result.returncode:
            detail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
            raise MultilinuxError(f"podman container inspect --size failed: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MultilinuxError(f"podman size inspect returned invalid JSON: {exc}") from exc
        return {
            "size_rw_bytes": payload.get("SizeRw"),
            "size_root_fs_bytes": payload.get("SizeRootFs"),
        }

    def image_info(self, reference: str) -> dict:
        if "@sha256:" not in reference:
            raise MultilinuxError(f"refusing to inspect an unlocked image reference: {reference}")
        result = self.runner(
            ["podman", "image", "inspect", "--format", "{{json .}}", reference],
            capture=True, timeout=60)
        if result.returncode:
            detail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
            raise MultilinuxError(f"podman image inspect failed: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MultilinuxError(f"podman image inspect returned invalid JSON: {exc}") from exc
        return {
            "id": payload.get("Id"),
            "digest": payload.get("Digest"),
            "size_bytes": payload.get("Size"),
        }

    def exec(self, name: str, argv: list[str], *, project: str, env_id: str,
             workdir: str | None, env: dict[str, str], capture: bool, timeout: int | None,
             user: str | None = None) -> ExecResult:
        self._require_owned(name, project, env_id)
        result = self.runner(exec_argv(name, argv, workdir=workdir, env=env, user=user),
                             capture=capture, timeout=timeout)
        return ExecResult(result.returncode, result.stdout or "", result.stderr or "")

    def _require_owned(self, name: str, project: str, env_id: str) -> ContainerState:
        info = self.inspect(name)
        if info is None:
            raise MultilinuxError(f"container {name} does not exist")
        if info.project != project or info.env_id != env_id or info.role != ROLE_ENVIRONMENT:
            raise MultilinuxError(
                f"refusing to touch {name}: it is not environment {env_id} of this project")
        return info

    def _check(self, argv: list[str], *, timeout: int, action: str) -> None:
        result = self.runner(argv, capture=True, timeout=timeout)
        if result.returncode:
            detail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
            raise MultilinuxError(f"podman {action} failed: {detail}")

    def _ensure_rootless(self) -> None:
        if not self.check_rootless or self._rootless_ok:
            return
        import sys
        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from tools import lab
        lab.require_rootless()
        self._rootless_ok = True


def decode_output(blob: bytes | str | None) -> str:
    """Podman and package tools sometimes emit bytes that are not UTF-8."""
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    return blob.decode("utf-8", errors="replace")


def _run_podman(argv: list[str], *, capture: bool, timeout: int | None):
    try:
        result = subprocess.run(argv, capture_output=capture, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MultilinuxError(f"{argv[0]} failed: {exc}") from exc
    if capture:
        result.stdout = decode_output(result.stdout)
        result.stderr = decode_output(result.stderr)
    return result
