"""Rootless Podman adapter for project-owned environments.

Every start, exec, stop and remove addresses one container by exact name and
refuses the call unless that container carries this project's environment
labels. There is no name-prefix or "all containers" operation.
"""

from dataclasses import dataclass, replace
import json
import fcntl
import os
from pathlib import Path
import re
import secrets
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time

from unifier.cuda_device import CUDA_DIRECTORY, CUDA_LABEL, CUDA_LIBRARY, CudaDevice, require_cuda_identity
from unifier.errors import UnifierError
from unifier.exec_control import CONTROL_GUEST_FD, control_exec_argv
from unifier.share_label import PRESERVE_HOST, volume_options
from unifier.wayland import WaylandRefresh, is_managed_wayland_bind, start_with_refresh


LABEL_PROJECT = "multilinux.project"
LABEL_ENV = "multilinux.env"
LABEL_ROLE = "multilinux.role"
ROLE_ENVIRONMENT = "environment"

# Exec-form entrypoint is not resolved through PATH. It runs under --init
# (catatonit) so a stop signal reaches it without waiting out the stop timeout.
SLEEPER = "/usr/bin/sleep"


@dataclass(frozen=True)
class CreateSpec:
    name: str
    env_id: str
    project: str
    image: str
    mounts: tuple[tuple[str, str], ...]
    # Ordinary shared host directories default to preserving host labels.
    # "shared-relabel" is the explicit, historical :z request.
    share_label_policy: str = PRESERVE_HOST
    network: str = "none"
    # Session sockets are mounted without a relabel. :z would change the
    # compositor's own label on the host.
    session_mounts: tuple[tuple[str, str], ...] = ()
    selinux_label: str = "default"
    user_namespace: str = "rootless-default"
    devices: tuple[str, ...] = ()
    ipc: str = "private"
    cuda: CudaDevice | None = None


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
    bind_mounts: tuple[tuple[str, str, bool], ...] | None = None
    mounts: tuple[tuple[str, str, bool, str], ...] = ()
    cuda: str | None = None


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class GuestProcess:
    """Identity of the process actually running inside the container.

    A host ``podman exec`` process is only a proxy.  Its pid therefore never
    identifies the execution: the guest reports its own pid, kernel start time
    and session/process-group ids over a dedicated control fd before it execs
    the requested argv.  PID plus start time cannot be recycled by a later
    guest process.
    """
    pid: int
    start: int
    pgrp: int
    sid: int
    token: str
    ownership: str = 'process'


# The guest control channel is always FD 3.  Podman's portable
# ``--preserve-fds N`` carries host descriptors 3..N+2 with the same numbers, so
# FD 3 is the only slot that is both fixed and reachable on runc and crun hosts.
CONTROL_FD = CONTROL_GUEST_FD
CONTROL_READY_SECONDS = 15.0
APPLICATION_BOOTSTRAP = r'''
source=$1
shift
for candidate in /usr/bin/python3 /bin/python3; do
    if [ -x "$candidate" ]; then exec "$candidate" -c "$source" "$@"; fi
done
case "$1" in
    run|run-stop-children) printf 'ERROR application-python3-unavailable\n' >&"$4" ;;
esac
exit 125
'''


def application_guest_argv(mode, token, *args, state_root=''):
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise UnifierError('application execution token is malformed')
    source = Path(__file__).with_name('application_supervisor.py').read_text()
    return ['/bin/sh', '-c', APPLICATION_BOOTSTRAP, 'unifier-application',
            source, mode, token, state_root, *map(str, args)]
# Fixed guest shell programs.  They use only /bin/sh and /proc; no host helper
# binary, interpreter or ABI is assumed to work inside the guest.
GUEST_WRAPPER = r'''
set -u
token=$1
control_fd=$2
shift 2
case $control_fd in
    ''|*[!0-9]*) exit 125 ;;
esac
if [ ! -r /proc/$$/stat ]; then
    printf 'ERROR proc-unavailable\n' >&"$control_fd"
    exit 125
fi
if ! read -r _ml_pid _ml_comm _ml_state _ml_ppid _ml_pgrp _ml_sid _ml_tty \
        _ml_tpgid _ml_flags _ml_minflt _ml_cminflt _ml_majflt _ml_cmajflt \
        _ml_utime _ml_stime _ml_cutime _ml_cstime _ml_priority _ml_nice \
        _ml_nthreads _ml_itreal _ml_start _ml_rest < /proc/$$/stat; then
    printf 'ERROR stat-unreadable\n' >&"$control_fd"
    exit 125
fi
case $_ml_start in
    ''|*[!0-9]*) printf 'ERROR stat-unparsed\n' >&"$control_fd"; exit 125 ;;
esac
case $_ml_pgrp in
    ''|*[!0-9]*) printf 'ERROR pgrp-unparsed\n' >&"$control_fd"; exit 125 ;;
esac
case $_ml_sid in
    ''|*[!0-9]*) printf 'ERROR sid-unparsed\n' >&"$control_fd"; exit 125 ;;
esac
printf 'READY pid=%s start=%s pgrp=%s sid=%s token=%s\n' \
    "$$" "$_ml_start" "$_ml_pgrp" "$_ml_sid" "$token" >&"$control_fd" || exit 125
eval "exec $control_fd>&-"
exec "$@"
'''

GUEST_SETSID_BOOTSTRAP = r'''
set -u
wrapper=$1
token=$2
control_fd=$3
shift 3
for candidate in /usr/bin/setsid /usr/sbin/setsid /bin/setsid \
        /usr/local/bin/setsid /usr/local/sbin/setsid; do
    if [ -x "$candidate" ] && "$candidate" --wait true >/dev/null 2>&1; then
        # --wait is essential: if setsid has to fork, its parent must stay the
        # process podman tracks until the real session leader exits.
        exec "$candidate" --wait /bin/sh -c "$wrapper" ml-session \
            "$token" "$control_fd" "$@"
    fi
done
printf 'ERROR setsid-wait-unavailable\n' >&"$control_fd"
exit 125
'''

GUEST_PROBE = r'''
pid=$1
want=$2
path=/proc/$pid/stat
if [ ! -e "$path" ]; then printf 'GONE\n'; exit 0; fi
if [ ! -r "$path" ]; then printf 'UNKNOWN\n'; exit 0; fi
if ! IFS= read -r line < "$path" 2>/dev/null; then printf 'UNKNOWN\n'; exit 0; fi
line=${line##*)}
set -- $line
state=${1:-}
start=${20:-}
if [ -z "$start" ]; then printf 'UNKNOWN\n'; exit 0; fi
if [ "$start" != "$want" ]; then printf 'REPLACED\n'; exit 0; fi
if [ "$state" = Z ]; then printf 'GONE\n'; else printf 'ALIVE\n'; fi
'''

GUEST_SIGNAL = r'''
target_pid=$1
target_start=$2
target_signal=$3
target_mode=$4
path=/proc/$target_pid/stat
if [ ! -e "$path" ]; then printf 'GONE\n'; exit 0; fi
if [ ! -r "$path" ]; then printf 'UNKNOWN\n'; exit 0; fi
if ! IFS= read -r line < "$path" 2>/dev/null; then printf 'UNKNOWN\n'; exit 0; fi
line=${line##*)}
set -- $line
state=${1:-}
pgrp=${3:-}
sid=${4:-}
start=${20:-}
if [ -z "$start" ]; then printf 'UNKNOWN\n'; exit 0; fi
if [ "$start" != "$target_start" ]; then printf 'REPLACED\n'; exit 0; fi
if [ "$state" = Z ]; then printf 'GONE\n'; exit 0; fi
if [ "$target_mode" = session ] && [ "$sid" = "$target_pid" ] && [ "$target_pid" -gt 1 ]; then
    # Every process in this exec's own session belongs to this execution,
    # including background jobs from a shell.  Never touch another session.
    for directory in /proc/[0-9]*; do
        member=${directory##*/}
        [ "$member" = "$$" ] && continue
        [ -r "$directory/stat" ] || continue
        if ! IFS= read -r member_line < "$directory/stat" 2>/dev/null; then continue; fi
        member_line=${member_line##*)}
        set -- $member_line
        member_state=${1:-}
        member_sid=${4:-}
        [ "$member_sid" = "$target_pid" ] || continue
        [ "$member_state" = Z ] && continue
        kill -s "$target_signal" "$member" 2>/dev/null || true
    done
elif [ "$target_mode" = group ] && [ "$pgrp" = "$target_pid" ] && [ "$target_pid" -gt 1 ]; then
    kill -s "$target_signal" -- "-$target_pid" 2>/dev/null || exit 6
else
    kill -s "$target_signal" "$target_pid" 2>/dev/null || exit 6
fi
printf 'SIGNALLED\n'
'''

GUEST_CLEANUP = r'''
target=$1
mode=${2:-session}
[ "$target" -gt 1 ] 2>/dev/null || { printf 'UNSUPPORTED\n'; exit 2; }
case $mode in
    session|group) ;;
    *) printf 'UNSUPPORTED\n'; exit 2 ;;
esac
session_members() {
    for directory in /proc/[0-9]*; do
        member=${directory##*/}
        [ "$member" = "$$" ] && continue
        [ -r "$directory/stat" ] || continue
        if ! IFS= read -r line < "$directory/stat" 2>/dev/null; then continue; fi
        line=${line##*)}
        set -- $line
        state=${1:-}
        pgrp=${3:-}
        sid=${4:-}
        if [ "$mode" = group ]; then
            [ "$pgrp" = "$target" ] || continue
        else
            [ "$sid" = "$target" ] || continue
        fi
        [ "$state" = Z ] && continue
        printf '%s\n' "$member"
    done
}
remaining=$(session_members)
if [ -n "$remaining" ]; then
    for member in $remaining; do
        kill -s TERM "$member" 2>/dev/null || true
    done
    sleep 1
    remaining=$(session_members)
    if [ -n "$remaining" ]; then
        for member in $remaining; do
            kill -s KILL "$member" 2>/dev/null || true
        done
        sleep 1
        remaining=$(session_members)
    fi
fi
if [ -n "$remaining" ]; then
    printf 'SURVIVORS\n'
else
    printf 'CLEANED\n'
fi
'''


# Native package tools may give dpkg its own PTY/session. Freeze the exact
# execution and discover its descendants by PPID before signalling anything;
# session-only cancellation would miss those nested sessions.
GUEST_PACKAGE_CANCEL = r'''
target=$1
expected=$2
read_identity() {
    [ -r "/proc/$1/stat" ] || return 1
    IFS= read -r line < "/proc/$1/stat" 2>/dev/null || return 1
    line=${line##*)}
    set -- $line
    state=$1
    parent=$2
    stamp=${20:-}
    [ -n "$stamp" ]
}
if ! read_identity "$target" || [ "$stamp" != "$expected" ] || [ "$state" = Z ]; then
    printf 'UNKNOWN\n'; exit 0
fi
kill -s STOP "$target" 2>/dev/null || { printf 'UNKNOWN\n'; exit 0; }
members="$target:$expected"
parents=" $target "
round=0
while :; do
    changed=no
    for directory in /proc/[0-9]*; do
        member=${directory##*/}
        case "$parents" in *" $member "*) continue ;; esac
        read_identity "$member" || continue
        case "$parents" in
            *" $parent "*)
                [ "$state" = Z ] && continue
                kill -s STOP "$member" 2>/dev/null || continue
                members="$members $member:$stamp"
                parents="$parents$member "
                changed=yes ;;
        esac
    done
    [ "$changed" = yes ] || break
    round=$((round+1))
    [ "$round" -lt 64 ] || { printf 'UNKNOWN\n'; exit 0; }
done
for entry in $members; do
    member=${entry%:*}; expected=${entry#*:}
    read_identity "$member" || continue
    [ "$stamp" = "$expected" ] || continue
    kill -s TERM "$member" 2>/dev/null || true
    kill -s CONT "$member" 2>/dev/null || true
done
sleep 1
for entry in $members; do
    member=${entry%:*}; expected=${entry#*:}
    read_identity "$member" || continue
    [ "$stamp" = "$expected" ] && [ "$state" != Z ] || continue
    kill -s KILL "$member" 2>/dev/null || true
done
sleep 1
for entry in $members; do
    member=${entry%:*}; expected=${entry#*:}
    read_identity "$member" || continue
    if [ "$stamp" = "$expected" ] && [ "$state" != Z ]; then
        printf 'UNKNOWN\n'; exit 0
    fi
done
printf 'GONE\n'
'''


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

    The container runs with --init so PID 1 is a real init. Without it the
    sleeper never handles SIGTERM (podman stop then waits the whole timeout and
    the stop path blocks the next start) and orphaned exec children stay as
    zombies under PID 1. With it, stop signals are forwarded and orphans are
    reaped inside the container.
    """
    if "@sha256:" not in spec.image:
        raise UnifierError(f"refusing to create from an unlocked image reference: {spec.image}")
    if spec.network not in {"none", "pasta"}:
        raise UnifierError("environment network must be none or pasta")
    if spec.selinux_label not in {"default", "disable"}:
        raise UnifierError("selinux label must be default or disable")
    if spec.ipc not in {"private", "host"}:
        raise UnifierError("ipc mode must be private or host")
    if spec.user_namespace not in {"rootless-default", "keep-id"}:
        raise UnifierError("user namespace must be rootless-default or keep-id")
    share_options = volume_options(spec.share_label_policy)
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
        "--init",
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
            raise UnifierError("desktop device must be an explicit /dev/videoN camera or /dev/dri device")
        argv.extend(["--device", f"{device}:{device}:rw"])
    if spec.cuda is not None:
        if (spec.devices or spec.session_mounts or spec.selinux_label != 'default'
                or spec.ipc != 'private' or spec.user_namespace != 'rootless-default'):
            raise UnifierError('CUDA requires a headless private environment with its own device list')
        for device in spec.cuda.nodes:
            argv.extend(['--device', f'{device}:{device}:rw'])
        argv.extend(['--label', CUDA_LABEL + '=' + json.dumps(spec.cuda.identity(), separators=(',', ':')),
                     '--volume', spec.cuda.library + ':' + CUDA_LIBRARY + ':ro,nosuid,nodev',
                     '--env', 'LD_LIBRARY_PATH=' + CUDA_DIRECTORY])
    for host, container in spec.mounts:
        volume = f"{host}:{container}"
        if share_options:
            volume += ":" + share_options
        argv.extend(["--volume", volume])
    for host, container in spec.session_mounts:
        if ":" in host or ":" in container:
            raise UnifierError("session mount paths cannot contain ':'")
        argv.extend(["--volume", f"{host}:{container}"])
    if spec.selinux_label == "disable":
        argv.extend(["--security-opt", "label=disable"])
    argv.extend([spec.image, "infinity"])
    return argv


def exec_argv(name: str, argv: list[str], *, workdir: str | None, env: dict[str, str],
              user: str | None = None, interactive: bool = False,
              tty: bool = False, control_fd: int | None = None,
              env_from_process: bool = False) -> list[str]:
    """Exec argv directly. Only the allowlisted variables are passed through.

    ``-i`` keeps stdin connected for pipes. ``-t`` is deliberately separate:
    podman merges stdout/stderr and can hang a pipe when a TTY is allocated, so
    callers must request it only for a real interactive terminal.
    ``control_fd`` asks for the guest control descriptor that keeps the identity
    handshake out of stdout and stderr.  It is only ever ``CONTROL_FD`` (3):
    rootless podman on Ubuntu 24.04 (runc) knows only ``--preserve-fds N``, which
    passes host FDs 3..N+2 to the container with the same numbers, and rejects
    the singular ``--preserve-fd`` newer podman added.  ``--preserve-fds 1`` plus
    a host FD 3 is the one shape that works on runc and crun alike, and it hands
    over exactly one descriptor instead of every FD below N.
    """
    if not argv:
        raise UnifierError("argv is empty")
    command = ["podman", "exec"]
    if interactive or tty:
        command.append("-i")
    if tty:
        command.append("-t")
    if user is not None:
        command.extend(["--user", user])
    if control_fd is not None:
        if isinstance(control_fd, bool) or not isinstance(control_fd, int) or control_fd < 3:
            raise UnifierError("control descriptor must be an integer >= 3")
        if control_fd != CONTROL_FD:
            raise UnifierError(
                "the guest control descriptor is fixed at "
                f"{CONTROL_FD}; only --preserve-fds 1 is portable across runtimes")
        command.extend(["--preserve-fds", "1"])
    if workdir is not None:
        command.extend(["--workdir", workdir])
    for key, value in env.items():
        command.extend(["--env", key if env_from_process else f"{key}={value}"])
    command.append(name)
    command.extend(argv)
    return command


def host_exec_command(write_fd: int, command: list[str]) -> list[str]:
    """Wrap a podman exec so the caller's control pipe becomes guest FD 3.

    The caller keeps its own FD and hands the child exactly one inherited
    descriptor with ``pass_fds``.  The helper (``unifier.exec_control``) does the
    dup2 in its own already-forked, single-threaded process and execs podman, so
    the proxy stays one process and keeps its exit status and signals.  The host
    parent never dup2s FD 3, which would race other threads between fork and
    exec, and nothing uses ``preexec_fn``.
    """
    try:
        return control_exec_argv(write_fd, command)
    except ValueError as exc:
        error = UnifierError(f"cannot build control exec command: {exc}")
        error.execution_started = False  # Rejected before any proxy/guest spawn.
        raise error from exc


def guest_environment_fd(env: dict[str, str]) -> int:
    """Make a private anonymous Podman env-file without changing its host env.

    In particular, guest HOME/XDG_RUNTIME_DIR must not redirect the host
    client's storage or connection lookup. Values also stay out of argv and
    persistent files. Only the control descriptor is preserved into the guest.
    """
    lines = []
    for key, value in env.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
                or not isinstance(value, str) or any(c in value for c in '\0\n\r')):
            error = UnifierError('invalid guest environment entry')
            error.execution_started = False
            raise error
        lines.append(f'{key}={value}\n')
    data = ''.join(lines).encode()
    if len(data) > 1024 * 1024:
        error = UnifierError('guest environment exceeds 1 MiB')
        error.execution_started = False
        raise error
    fd = os.memfd_create('unifier-guest-env', os.MFD_CLOEXEC)
    try:
        with os.fdopen(os.dup(fd), 'wb') as stream:
            stream.write(data)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def guest_session_argv(argv: list[str], *, token: str, control_fd: int,
                        tty: bool) -> list[str]:
    """Build the fixed guest wrapper that reports real execution identity.

    A non-TTY execution gets its own session through ``setsid --wait`` so the
    whole command tree can be signalled and cleaned without touching other
    sessions in the shared container.  A requested TTY must keep the podman pty
    as its controlling terminal, so the wrapper only records the ids the guest
    kernel assigned and execs the argv unchanged.

    ``control_fd`` is the descriptor number *inside* the container.  A real exec
    always uses ``CONTROL_FD`` (3), because that is the number
    ``--preserve-fds 1`` delivers; a host-local test may use any inherited
    descriptor >= 3.
    """
    if not argv:
        raise UnifierError("guest session argv is empty")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise UnifierError("guest session token is malformed")
    if isinstance(control_fd, bool) or not isinstance(control_fd, int) or control_fd < 3:
        raise UnifierError("guest control descriptor must be an integer >= 3")
    inner = ["/bin/sh", "-c", GUEST_WRAPPER, "ml-session", token,
             str(control_fd), *argv]
    if tty:
        return inner
    return ["/bin/sh", "-c", GUEST_SETSID_BOOTSTRAP, "ml-session",
            GUEST_WRAPPER, token, str(control_fd), *argv]


def _host_exit_status(code: int) -> int:
    return code if code >= 0 else 128 - code


def parse_guest_ready(line: str, token: str) -> GuestProcess:
    if not isinstance(line, str) or not line.endswith("\n"):
        raise UnifierError("guest execution did not report a complete identity line")
    parts = line.split()
    if not parts or parts[0] != "READY":
        raise UnifierError(
            "guest execution identity was not reported; refusing an untracked execution")
    values = {}
    for item in parts[1:]:
        if "=" not in item:
            raise UnifierError("guest execution identity line is malformed")
        key, value = item.split("=", 1)
        values[key] = value
    if values.get("token") != token:
        raise UnifierError("guest execution identity token mismatch")
    parsed = {}
    for key in ("pid", "start", "pgrp", "sid"):
        try:
            number = int(values[key], 10)
        except (KeyError, ValueError) as exc:
            raise UnifierError(
                f"guest execution identity has no valid {key}") from exc
        if number <= 0:
            raise UnifierError(f"guest execution identity has invalid {key}")
        parsed[key] = number
    if parsed["pid"] == 1:
        raise UnifierError("guest execution cannot use container PID 1")
    return GuestProcess(pid=parsed["pid"], start=parsed["start"],
                        pgrp=parsed["pgrp"], sid=parsed["sid"], token=token)


class SessionExecution:
    """One live guest execution and the host proxy that carries its streams."""

    def __init__(self, runtime: "PodmanRuntime", *, name: str, project: str,
                 env_id: str, process, guest: GuestProcess,
                 user: str | None = None):
        self.runtime = runtime
        self.name = name
        self.project = project
        self.env_id = env_id
        self.process = process
        self.guest = guest
        self.user = user

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def resize(self) -> None:
        """Have the owned Podman proxy copy the host TTY's current size.

        SIGWINCH to the guest alone does not change its PTY dimensions.
        Podman must read the inherited terminal and resize the guest PTY.
        This notification is separate from targeted guest cancellation.
        """
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGWINCH)
            except ProcessLookupError:
                pass

    def probe(self) -> str:
        if self.guest.ownership == 'subreaper-v1':
            return self.runtime.probe_application_guest(
                self.name, self.guest, project=self.project, env_id=self.env_id, user=self.user)
        return self.runtime.probe_guest(
            self.name, self.guest, project=self.project, env_id=self.env_id,
            user=self.user)

    def signal(self, signum) -> str:
        if self.guest.ownership == 'subreaper-v1':
            return self.runtime.signal_application_guest(
                self.name, self.guest, signum, project=self.project, env_id=self.env_id, user=self.user)
        return self.runtime.signal_guest(
            self.name, self.guest, signum, project=self.project, env_id=self.env_id,
            user=self.user)

    def cleanup(self) -> str:
        if self.guest.ownership == 'subreaper-v1':
            # Never kill background descendants merely because a launcher or
            # host proxy returned. Only the terminal receipt proves completion.
            return self.probe()
        return self.runtime.cleanup_guest(
            self.name, self.guest, project=self.project, env_id=self.env_id,
            user=self.user)

    def abort_proxy(self) -> None:
        """Abort only the host proxy when the guest cannot be signalled.

        This is never treated as guest cancellation by ProjectSessions.
        """
        self.runtime._kill_proxy_group(self.process)


def container_state_from_inspect(name: str, data: dict) -> ContainerState:
    labels = (data.get("Config") or {}).get("Labels") or {}
    state = data.get("State") or {}
    mounts = data.get("Mounts")
    binds = None
    if isinstance(mounts, list):
        binds = tuple((mount["Source"], mount["Destination"], mount["RW"])
                      for mount in mounts
                      if isinstance(mount, dict) and mount.get("Type") == "bind"
                      and isinstance(mount.get("Source"), str)
                      and isinstance(mount.get("Destination"), str)
                      and isinstance(mount.get("RW"), bool))
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
        bind_mounts=binds,
        cuda=labels.get(CUDA_LABEL),
        mounts=tuple(
            (item.get("Source") or "", item["Destination"],
             item.get("RW") is True, item.get("Type") or "")
            for item in (mounts if isinstance(mounts, list) else [])
            if isinstance(item, dict) and isinstance(item.get("Destination"), str)
        ),
    )


def validate_created_spec(spec: CreateSpec, expected_cid: str, data: dict,
                          image: dict, saved: dict) -> None:
    """Verify the supported unused rootless layout without starting it.

    ImageDigest can name another manifest alias of the same cached image. The
    locked reference must resolve to the exact image ID used by the container.
    Resource quotas belong to the caller's execution policy, not CreateSpec.
    """
    if (not re.fullmatch(r'[0-9a-f]{64}', expected_cid) or spec.cuda is not None
            or spec.session_mounts or spec.selinux_label != 'default'
            or spec.user_namespace != 'rootless-default' or spec.ipc != 'private'
            or spec.share_label_policy != PRESERVE_HOST):
        raise UnifierError('resume-create supports only an explicit headless rootless layout')
    cfg, hc, state = data.get('Config', {}), data.get('HostConfig', {}), data.get('State', {})
    labels = cfg.get('Labels') or {}
    expected_labels = {LABEL_PROJECT: spec.project, LABEL_ENV: spec.env_id, LABEL_ROLE: ROLE_ENVIRONMENT}
    image_id = str(image.get('Id', '')).removeprefix('sha256:')
    if (data.get('Id') != expected_cid or data.get('Name') != spec.name
            or {key: value for key, value in labels.items() if key.startswith('multilinux.')} != expected_labels
            or not re.fullmatch(r'[0-9a-f]{64}', image_id)
            or str(data.get('Image', '')).removeprefix('sha256:') != image_id
            or data.get('ImageName') != spec.image or cfg.get('Image') != spec.image
            or spec.image not in (image.get('RepoDigests') or [])
            or image.get('Os') != 'linux' or image.get('Architecture') != 'amd64'):
        raise UnifierError('resume-create container ownership or locked image differs')
    zero_time = '0001-01-01T00:00:00Z'
    # conmon can report a pre-start failure while the container remains unused.
    # Keep that bounded diagnostic intact; execution history below is decisive.
    diagnostic = state.get('Error', '')
    if (state.get('Status') != 'created' or state.get('Running') is not False
            or state.get('Pid') != 0 or state.get('StartedAt') != zero_time
            or state.get('FinishedAt') != zero_time or state.get('ExitCode') != 0
            or not isinstance(diagnostic, str) or len(diagnostic) > 4096
            or any(state.get(key) for key in ('Paused', 'Restarting', 'OOMKilled', 'Dead'))
            or data.get('RestartCount') != 0 or data.get('ExecIDs')
            or data.get('Pod') or data.get('Dependencies') or data.get('IsInfra') or data.get('IsService')):
        raise UnifierError('resume-create requires a never-started created container')
    if (cfg.get('Entrypoint') != [SLEEPER] or cfg.get('Cmd') != ['infinity']
            or data.get('Path') != '/run/podman-init' or data.get('Args') != ['--', SLEEPER, 'infinity']
            or cfg.get('User') not in ('', '0', '0:0') or cfg.get('WorkingDir') not in ('', '/')
            or cfg.get('StopTimeout') != 8 or hc.get('Init') is not True
            or hc.get('NetworkMode') != spec.network or hc.get('IpcMode') != spec.ipc
            or hc.get('PidMode') not in ('', 'private') or hc.get('UTSMode') not in ('', 'private')
            or hc.get('UsernsMode') not in ('', 'private', 'host')
            or hc.get('RestartPolicy', {}).get('Name') != 'no'
            or any(hc.get(key) for key in ('Privileged', 'AutoRemove', 'ReadonlyRootfs', 'SecurityOpt',
                'CapAdd', 'CapDrop', 'GroupAdd', 'VolumesFrom', 'PublishAllPorts', 'PortBindings',
                'Devices', 'DeviceRequests', 'DeviceCgroupRules', 'CDIDevices', 'Tmpfs'))):
        raise UnifierError('resume-create command or namespace configuration differs')
    expected_env = set((image.get('Config') or {}).get('Env') or []) | {'container=podman'}
    # A failed pre-start attempt can persist Podman's defaults in inspect even
    # while the saved OCI environment remains the original image environment.
    defaults = {'HOME=/root', 'HOSTNAME=' + expected_cid[:12]}
    if (cfg.get('Hostname') != expected_cid[:12]
            or not expected_env <= set(cfg.get('Env') or []) <= expected_env | defaults):
        raise UnifierError('resume-create environment differs from the cached image')
    mounts = data.get('Mounts') or []
    if (len(mounts) != len(spec.mounts)
            or sorted((m.get('Source'), m.get('Destination')) for m in mounts) != sorted(spec.mounts)
            or any(m.get('Type') != 'bind' or m.get('RW') is not True
                   or m.get('Mode') not in ('', None) for m in mounts)):
        raise UnifierError('resume-create shared mounts differ')
    if (saved.get('id') != expected_cid or saved.get('name') != spec.name
            or saved.get('staticDir') != data.get('StaticDir')
            or saved.get('rootfsImageID') != image_id or saved.get('rootfsImageName') != spec.image):
        raise UnifierError('resume-create saved native identity differs')
    oci = saved['spec']
    linux, process = oci.get('linux', {}), oci.get('process', {})
    namespaces = linux.get('namespaces') or []
    namespace_types = [item.get('type') for item in namespaces]
    if (oci.get('hooks') or linux.get('devices')
            or not {'pid', 'network', 'ipc', 'uts', 'mount'} <= set(namespace_types)
            or len(namespace_types) != len(set(namespace_types))
            or set(namespace_types) - {'pid', 'network', 'ipc', 'uts', 'mount', 'cgroup', 'user'}
            or any(item.get('path') for item in namespaces)
            or process.get('args') != ['/run/podman-init', '--', SLEEPER, 'infinity']
            or process.get('user') != {'uid': 0, 'gid': 0}
            or set(process.get('env') or []) != expected_env):
        raise UnifierError('resume-create saved rootless execution layout differs')
    shares, devices, init_count = [], [], 0
    for mount in oci.get('mounts', []):
        if mount.get('type') != 'bind':
            continue
        pair = (mount.get('source'), mount.get('destination'))
        options = set(mount.get('options') or [])
        if pair in spec.mounts:
            if options != {'rprivate', 'rbind'}:
                raise UnifierError('resume-create saved shared mount options differ')
            shares.append(pair)
        elif pair[0] in spec.devices and pair[1] == pair[0]:
            if options != {'slave', 'nosuid', 'noexec', 'rw', 'rbind'}:
                raise UnifierError('resume-create saved device permissions differ')
            devices.append(pair[0])
        elif (pair[0] in {'/usr/libexec/podman/catatonit', '/usr/libexec/catatonit/catatonit', '/usr/bin/catatonit'}
              and pair[1] == '/run/podman-init' and options == {'bind', 'ro', 'private'}):
            init_count += 1
        else:
            raise UnifierError('resume-create has an unexpected saved host bind')
    if sorted(shares) != sorted(spec.mounts) or sorted(devices) != sorted(spec.devices) or init_count != 1:
        raise UnifierError('resume-create saved mount/device set differs')


class PodmanRuntime:
    def __init__(self, runner=None, *, check_rootless=True, popen=None):
        self.runner = runner or _run_podman
        self.popen = popen or subprocess.Popen
        self.check_rootless = check_rootless
        self._rootless_ok = False
        self.application_state_root = ''

    def read_inventory_readonly(self, record):
        """Read a supported stopped DB without starting or execing the peer.

        The helper acquires/releases one journaled storage mount reference.
        Running peers, unsupported layouts and ambiguous storage journals refuse
        before any package write. Real use requires T2 storage admission.
        """
        from unifier.package_inventory import (
            InventoryObservation, MAX_INVENTORY_BYTES, observation,
        )
        from unifier.inventory_backends import backend_for
        from unifier.package_inventory import InventoryUnsupported
        from unifier.view_journal import read_journal
        try:
            database, _, _ = backend_for(record, require_available=True)
        except InventoryUnsupported as exc:
            return observation(record, status='unsupported', database='unknown', reason=str(exc))
        runtime = record['runtime']
        cid = runtime['container_id']
        before = self._require_owned(runtime['container_name'], runtime['project'], record['id'])
        if before.container_id != cid:
            return observation(record, status='unavailable', database=database, reason='peer container identity changed')
        if before.running or before.status not in {'exited', 'created', 'configured', 'stopped'}:
            return observation(record, status='unsupported', database=database, reason='running peer needs a consistent snapshot adapter')
        if not re.fullmatch(r'[0-9a-f]{64}', cid):
            return observation(record, status='unavailable', database=database, reason='peer requires a full container ID')
        directory = Path(runtime['project']) / '.lab/state/inventory-observations'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = directory / (cid + '.json')
        descriptor = os.open(directory / (cid + '.lock'),
                             os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return observation(record, status='unavailable', database=database, reason='peer inventory observer is busy')
            previous = read_journal(str(journal))
            if journal.exists() and (previous is None or previous.get('state') != 'released'):
                return observation(record, status='unavailable', database=database, reason='inventory storage journal needs reconciliation')
            token = secrets.token_hex(16)
            minimal = {key: record[key] for key in ('id', 'target', 'schema_version',
                                                   'environment_uuid', 'generation')}
            minimal['runtime'] = {key: runtime[key] for key in ('container_id', 'project')}
            result = self.runner(
                ['podman', 'unshare', sys.executable, str(Path(__file__).with_name('inventory_probe.py')),
                 '--record', json.dumps(minimal), '--journal', str(journal), '--token', token],
                capture=True, timeout=90)
            if result.returncode or len(result.stdout or '') > MAX_INVENTORY_BYTES + 131072:
                raise UnifierError('read-only inventory helper failed or exceeded output budget')
            value = InventoryObservation(**json.loads(result.stdout))
            final = read_journal(str(journal))
            if value.status == 'supported' and (not final or final.get('state') != 'released'
                    or final.get('token') != token or final.get('cid') != cid):
                raise UnifierError('inventory storage release is unverified')
            after = self._require_owned(runtime['container_name'], runtime['project'], record['id'])
            if (after.container_id != cid or after.running or after.status != before.status
                    or after.started_at != before.started_at or after.finished_at != before.finished_at):
                raise UnifierError('peer container state changed during observation')
            return value
        except (OSError, ValueError, TypeError, UnifierError) as exc:
            return observation(record, status='unavailable', database=database, reason=str(exc)[-500:])
        finally:
            os.close(descriptor)

    def recover_inventory_storage(self, record):
        """Explicit recovery under the service's management/lifecycle locks."""
        runtime = record['runtime']
        cid = runtime['container_id']
        info = self._require_owned(runtime['container_name'], runtime['project'], record['id'])
        if info.container_id != cid or info.running:
            raise UnifierError('inventory recovery requires the exact stopped instance')
        if not re.fullmatch(r'[0-9a-f]{64}', cid):
            raise UnifierError('inventory recovery requires a full CID')
        directory = Path(runtime['project']) / '.lab/state/inventory-observations'
        if not directory.exists():
            return {'status': 'not-needed'}
        descriptor = os.open(directory / (cid + '.lock'),
                             os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            minimal = {key: record[key] for key in ('id', 'target', 'schema_version',
                                                   'environment_uuid', 'generation')}
            minimal['runtime'] = {key: runtime[key] for key in ('container_id', 'project')}
            result = self.runner(
                ['podman', 'unshare', sys.executable, str(Path(__file__).with_name('inventory_probe.py')),
                 '--record', json.dumps(minimal), '--journal', str(directory / (cid + '.json')),
                 '--recover'], capture=True, timeout=40)
            if len(result.stdout or '') > 65536:
                raise UnifierError('inventory recovery output exceeds budget')
            value = json.loads(result.stdout)
            if not isinstance(value, dict) or 'status' not in value:
                raise UnifierError('inventory recovery returned an invalid result')
            if result.returncode and value['status'] != 'unresolved':
                raise UnifierError('inventory recovery helper failed')
            return value
        finally:
            os.close(descriptor)

    def create(self, spec: CreateSpec) -> str:
        self._ensure_rootless()
        if self.inspect(spec.name) is not None:
            raise UnifierError(f"container {spec.name} already exists")
        result = self.runner(creation_argv(spec), capture=True, timeout=60)
        if result.returncode:
            raise UnifierError(f"podman create failed: {(result.stderr or '')[-2000:]}")
        container_id = (result.stdout or "").strip()
        if not container_id:
            raise UnifierError("podman create did not return a container id")
        return container_id

    def verify_created(self, spec: CreateSpec, expected_container_id: str) -> ContainerState:
        """Read one exact unused container and its cached image; never adopt by name."""
        self._ensure_rootless()
        if not re.fullmatch(r'[0-9a-f]{64}', expected_container_id):
            raise UnifierError('resume-create requires one complete container ID')
        def read(argv):
            result = self.runner(argv, capture=True, timeout=20)
            if result.returncode or len(result.stdout or '') > 2 * 1024**2:
                raise UnifierError('resume-create native metadata is unavailable or too large')
            return json.loads(result.stdout)
        data, = read(['podman', 'inspect', '--format', 'json', expected_container_id])
        image = read(['podman', 'image', 'inspect', '--format', '{{json .}}', spec.image])
        saved = self._created_configuration(data, expected_container_id)
        validate_created_spec(spec, expected_container_id, data, image, saved)
        return container_state_from_inspect(spec.name, data)

    def _created_configuration(self, data: dict, cid: str) -> dict:
        # Before first start there is no live config.json. Read only this CID's
        # saved OCI row, without mounting storage or asking Podman to init it.
        directory = Path(data.get('StaticDir') or '')
        if (not directory.is_absolute() or '..' in directory.parts or directory.name != 'userdata'
                or directory.parent.name != cid or directory.parent.parent.name != 'overlay-containers'):
            raise UnifierError('resume-create native configuration path is unbound')
        database = directory.parents[2] / 'db.sql'
        value = database.lstat()
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid() or value.st_size > 128 * 1024**2:
            raise UnifierError('resume-create native configuration database is untrusted')
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            connection.execute('PRAGMA query_only=ON')
            row = connection.execute('SELECT JSON FROM ContainerConfig WHERE ID=? AND length(JSON)<=?',
                                     (cid, 2 * 1024**2)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise UnifierError('resume-create saved configuration is unavailable')
        return json.loads(row[0])

    def inspect(self, name: str) -> ContainerState | None:
        self._ensure_rootless()
        result = self.runner(["podman", "inspect", "--format", "json", name],
                             capture=True, timeout=20)
        if result.returncode:
            detail = (result.stderr or "") + (result.stdout or "")
            if "no such" in detail.lower():
                return None
            raise UnifierError(f"podman inspect failed: {detail[-1500:]}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UnifierError(f"podman inspect returned invalid JSON: {exc}") from exc
        if not payload:
            return None
        return container_state_from_inspect(name, payload[0])

    def start(self, name: str, *, project: str, env_id: str,
              wayland: WaylandRefresh | None = None) -> None:
        info = self._require_owned(name, project, env_id)
        if info.cuda is not None:
            try:
                identity = json.loads(info.cuda)
            except (TypeError, ValueError) as exc:
                raise UnifierError('CUDA container identity is invalid') from exc
            cuda = require_cuda_identity(identity)
            if (cuda.library, CUDA_LIBRARY, False) not in (info.bind_mounts or ()):
                raise UnifierError('CUDA driver bind is missing or writable')
            name = info.container_id
        if wayland is not None:
            if (name != info.container_id or not re.fullmatch(r"[0-9a-f]{64}", name)
                    or not is_managed_wayland_bind(info.mounts, wayland.old)):
                raise UnifierError("container identity or saved Wayland bind changed before start")
            try:
                start_with_refresh(self._check, name, wayland)
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot refresh Wayland mount: {exc}") from exc
            return
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
            raise UnifierError(f"podman container inspect --size failed: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UnifierError(f"podman size inspect returned invalid JSON: {exc}") from exc
        return {
            "size_rw_bytes": payload.get("SizeRw"),
            "size_root_fs_bytes": payload.get("SizeRootFs"),
        }

    def pull_image(self, reference: str, *, platform: str = "linux/amd64") -> dict:
        """Pull exactly one already locked reference; never resolves a tag."""
        if "@sha256:" not in reference:
            raise UnifierError(
                f"refusing to pull an unlocked image reference: {reference}")
        self._ensure_rootless()
        self._check(["podman", "pull", "--platform", platform, reference],
                    timeout=900, action="pull")
        return self.image_info(reference)

    def image_info(self, reference: str) -> dict:
        if "@sha256:" not in reference:
            raise UnifierError(f"refusing to inspect an unlocked image reference: {reference}")
        result = self.runner(
            ["podman", "image", "inspect", "--format", "{{json .}}", reference],
            capture=True, timeout=60)
        if result.returncode:
            detail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
            raise UnifierError(f"podman image inspect failed: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UnifierError(f"podman image inspect returned invalid JSON: {exc}") from exc
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

    def package_exec(self, name: str, argv: list[str], *, project: str, env_id: str,
                     env: dict[str, str], interactive: bool, stream: bool,
                     timeout: int | None, on_guest=None) -> ExecResult:
        from unifier.native_io import run_native
        info = self._require_owned(name, project, env_id)
        # Pin the exec to the inspected instance, and explicitly use guest root
        # even in keep-id containers. No host package tool is involved.
        # The identity handshake rides the single --preserve-fds 1 descriptor, so
        # the native transaction reaches DNF/dnf5/apt instead of dying in an
        # "unknown flag" exec failure on an older runc host.
        read_fd, write_fd = os.pipe()
        token = secrets.token_hex(16)
        identity = None
        write_open = True
        guest = guest_session_argv(argv, token=token, control_fd=CONTROL_FD, tty=interactive)
        command = host_exec_command(write_fd, exec_argv(
            info.container_id, guest, workdir=None, env=env, user="0",
            interactive=interactive, tty=interactive, control_fd=CONTROL_FD))

        def spawn(command, **kwargs):
            nonlocal identity, write_open
            process = self.popen(command, pass_fds=(write_fd,), **kwargs)
            os.close(write_fd)
            write_open = False
            try:
                identity = self._read_guest_identity(read_fd, token, CONTROL_READY_SECONDS)
                if on_guest is not None:
                    on_guest(identity)
            except BaseException:
                self._kill_proxy_group(process)
                raise
            return process

        def abort():
            # A host proxy is not the package transaction. Cancel the exact
            # guest execution before releasing the package-operation lock.
            if identity is not None:
                return self.cancel_package_guest(info.container_id, identity, project=project, env_id=env_id)
            return "unknown"

        try:
            result = run_native(command, interactive=interactive, stream=stream, timeout=timeout,
                                spawn=spawn, abort=abort)
        finally:
            if write_open:
                os.close(write_fd)
            os.close(read_fd)
        return ExecResult(result.returncode, result.stdout, result.stderr)

    def cancel_package_guest(self, name: str, guest: GuestProcess, *, project: str,
                             env_id: str) -> str:
        try:
            self._require_owned(name, project, env_id)
            result = self.runner(exec_argv(name, ["/bin/sh", "-c", GUEST_PACKAGE_CANCEL,
                "unifier-package-cancel", str(guest.pid), str(guest.start)],
                workdir=None, env={}, user="0"), capture=True, timeout=15)
        except (UnifierError, OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
        return "gone" if result.returncode == 0 and result.stdout.strip() == "GONE" else "unknown"

    def start_session_exec(self, name: str, argv: list[str], *, project: str,
                           env_id: str, workdir: str | None, env: dict[str, str],
                           user: str | None = None, interactive: bool = True,
                           tty: bool = False, on_spawn=None,
                           control_timeout: float = CONTROL_READY_SECONDS,
                           ownership: str = 'process', execution_token: str | None = None,
                           application_exit_policy: str = 'wait'
                           ) -> SessionExecution:
        """Start one exec and require a real guest identity before returning.

        No session is handed to a supervisor until the guest has reported its
        pid, start time, process group and session over the dedicated control
        fd. A proxy alone is not an execution identity. If readiness is not
        proven, the proxy is killed and the call fails closed;
        ProjectSessions keeps the lease unresolved instead of claiming a clean
        cancellation it cannot prove.

        The control fd is carried as the single ``--preserve-fds 1`` descriptor
        with the host write end remapped onto FD 3 by ``unifier.exec_control``,
        which is the only form both an old runc host (no ``--preserve-fd``) and a
        crun host accept.  READY never travels on stdout, so the proxy's stdout
        and stderr stay whatever the command produced.
        """
        if application_exit_policy not in ('wait', 'stop-children'):
            raise UnifierError('unsupported application exit policy')
        if application_exit_policy != 'wait' and ownership != 'subreaper-v1':
            raise UnifierError('application exit policy requires process-tree ownership')
        self._require_owned(name, project, env_id)
        if ownership not in {'process', 'subreaper-v1'}:
            raise UnifierError('unsupported execution ownership protocol')
        token = execution_token or secrets.token_hex(16)
        read_fd, write_fd = os.pipe()
        environment_fd = None
        process = None
        try:
            # ``pass_fds`` makes the write end inheritable in the child only.
            # Flipping the flag on the parent's own descriptor would leak this
            # pipe into unrelated subprocesses started by other threads.
            mode = 'run-stop-children' if application_exit_policy == 'stop-children' else 'run'
            guest = (application_guest_argv(mode, token, CONTROL_FD, *argv,
                                           state_root=self.application_state_root)
                     if ownership == 'subreaper-v1' else
                     guest_session_argv(argv, token=token, control_fd=CONTROL_FD, tty=tty))
            command = exec_argv(
                name, guest, workdir=workdir, env={}, user=user,
                interactive=interactive, tty=tty, control_fd=CONTROL_FD)
            if env:
                environment_fd = guest_environment_fd(env)
                command[2:2] = ['--env-file', f'/proc/self/fd/{environment_fd}']
            command = host_exec_command(write_fd, command)
            try:
                process = self.popen(
                    command, stdin=None, stdout=None, stderr=None,
                    process_group=0,
                    pass_fds=((write_fd, environment_fd) if environment_fd is not None
                              else (write_fd,)), env=dict(os.environ))
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot start podman exec: {exc}") from exc
        except BaseException:
            if process is None:
                os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
            if environment_fd is not None:
                os.close(environment_fd)
        if on_spawn is not None:
            on_spawn(process)
        try:
            identity = replace(self._read_guest_identity(read_fd, token, control_timeout),
                               ownership=ownership)
        except BaseException:
            self._kill_proxy_group(process)
            os.close(read_fd)
            raise
        os.close(read_fd)
        return SessionExecution(self, name=name, project=project, env_id=env_id,
                                process=process, guest=identity, user=user)

    def start_application_exec(self, name, argv, *, token, exit_policy='wait', **kwargs):
        return self.start_session_exec(name, argv, ownership='subreaper-v1',
                                       execution_token=token,
                                       application_exit_policy=exit_policy, **kwargs)

    def observe_application(self, name, token, *, project, env_id, user=None):
        try:
            info = self._require_owned(name, project, env_id)
            if not info.running:
                return {'state': 'unknown', 'reason': 'container is not running'}
            result = self.runner(exec_argv(name, application_guest_argv('probe', token,
                state_root=self.application_state_root),
                workdir=None, env={}, user=user), capture=True, timeout=10)
            if result.returncode or len(result.stdout or '') > 16384:
                return {'state': 'unknown', 'reason': 'application observation failed'}
            value = json.loads(result.stdout)
            if (not isinstance(value, dict) or value.get('token') != token
                    or value.get('ownership') != 'subreaper-v1'):
                return {'state': 'unknown', 'reason': 'application observation binding changed'}
            if value.get('state') == 'exited' and value.get('all_children_reaped') is not True:
                return {'state': 'unknown', 'reason': 'terminal receipt incomplete'}
            return value
        except (OSError, ValueError, TypeError, UnifierError):
            return {'state': 'unknown', 'reason': 'application observation unavailable'}

    def probe_application_guest(self, name, guest, *, project, env_id, user=None):
        value = self.observe_application(name, guest.token, project=project, env_id=env_id, user=user)
        if value.get('pid') != guest.pid or value.get('start') != guest.start:
            return 'unknown'
        return {'running': 'alive', 'exited': 'gone'}.get(value.get('state'), 'unknown')

    def signal_application_guest(self, name, guest, signum, *, project, env_id, user=None):
        try:
            number = int(signal.Signals(signum))
            if number not in {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}:
                raise UnifierError('application cancellation supports TERM/INT/HUP only')
            self._require_owned(name, project, env_id)
            result = self.runner(exec_argv(name, application_guest_argv(
                'signal', guest.token, guest.pid, guest.start, number,
                state_root=self.application_state_root),
                workdir=None, env={}, user=user), capture=True, timeout=10)
            if result.returncode or len(result.stdout or '') > 16384:
                return 'unknown'
            return {'signalled': 'signalled', 'exited': 'gone'}.get(
                json.loads(result.stdout).get('state'), 'unknown')
        except (OSError, ValueError, TypeError, UnifierError):
            return 'unknown'

    def _read_guest_identity(self, read_fd: int, token: str,
                             timeout: float) -> GuestProcess:
        deadline = time.monotonic() + timeout
        data = b""
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UnifierError(
                    "guest execution did not report its identity in time; "
                    "refusing to claim an untracked execution was cancelled")
            readable, _writable, _errors = select.select([read_fd], [], [], remaining)
            if not readable:
                raise UnifierError(
                    "guest execution identity channel timed out before readiness")
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 4096:
                raise UnifierError("guest execution identity line is too long")
        if b"\n" not in data:
            raise UnifierError(
                "guest execution exited before reporting its identity")
        line = data.split(b"\n", 1)[0].decode("utf-8", "replace")
        if line.startswith("ERROR"):
            error = UnifierError(f"guest prerequisite failed: {line}")
            error.execution_started = False
            raise error
        return parse_guest_ready(line + "\n", token)

    @staticmethod
    def _kill_proxy_group(process) -> None:
        """Kill only the host proxy group after readiness failed.

        This does not pretend the guest stopped; callers must keep the
        resolution unresolved when no guest identity exists.
        """
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

    def session_exec(self, name: str, argv: list[str], *, project: str, env_id: str,
                     workdir: str | None, env: dict[str, str],
                     user: str | None = None, interactive: bool = True,
                     tty: bool = False, on_spawn=None, on_guest=None) -> int:
        """Compatibility wrapper for a foreground execution with guest control."""
        execution = self.start_session_exec(
            name, argv, project=project, env_id=env_id, workdir=workdir,
            env=env, user=user, interactive=interactive, tty=tty,
            on_spawn=on_spawn)
        if on_guest is not None:
            on_guest(execution.guest)
        try:
            code = execution.wait()
        except (OSError, ValueError) as exc:
            raise UnifierError(f"cannot wait for podman exec: {exc}") from exc
        return _host_exit_status(code)

    def probe_guest(self, name: str, guest: GuestProcess, *, project: str,
                    env_id: str, user: str | None = None, timeout: int = 10) -> str:
        """Return alive/gone/unknown using the guest's own /proc identity."""
        try:
            self._require_owned(name, project, env_id)
            result = self.runner(
                exec_argv(name, ["/bin/sh", "-c", GUEST_PROBE, "ml-probe",
                                 str(guest.pid), str(guest.start)],
                          workdir=None, env={}, user=user),
                capture=True, timeout=timeout)
        except (UnifierError, OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
        if result.returncode:
            return "unknown"
        answer = (result.stdout or "").strip()
        if answer == "ALIVE":
            return "alive"
        if answer in {"GONE", "REPLACED"}:
            return "gone"
        return "unknown"

    def signal_guest(self, name: str, guest: GuestProcess, signum, *,
                     project: str, env_id: str, user: str | None = None,
                     timeout: int = 10) -> str:
        """Target the exact guest execution, never the host proxy or container."""
        try:
            signal_name = signal.Signals(signum).name
        except (ValueError, AttributeError) as exc:
            raise UnifierError(f"unsupported guest signal: {signum!r}") from exc
        if signal_name.startswith("SIG"):
            signal_name = signal_name[3:]
        mode = "session" if guest.sid == guest.pid and guest.pid > 1 else "group"
        if mode == "group" and not (guest.pgrp == guest.pid and guest.pid > 1):
            mode = "pid"
        try:
            self._require_owned(name, project, env_id)
            result = self.runner(
                exec_argv(name, ["/bin/sh", "-c", GUEST_SIGNAL, "ml-signal",
                                 str(guest.pid), str(guest.start), signal_name, mode],
                          workdir=None, env={}, user=user),
                capture=True, timeout=timeout)
        except (UnifierError, OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
        answer = (result.stdout or "").strip()
        if answer == "SIGNALLED" and result.returncode == 0:
            return "signalled"
        if answer in {"GONE", "REPLACED"}:
            return "gone"
        return "unknown"

    def cleanup_guest(self, name: str, guest: GuestProcess, *, project: str,
                      env_id: str, user: str | None = None,
                      timeout: int = 15) -> str:
        """Clean only this execution's own guest session/group, then verify it."""
        if guest.sid == guest.pid and guest.pid > 1:
            mode = "session"
        elif guest.pgrp == guest.pid and guest.pid > 1:
            mode = "group"
        else:
            return "unsupported"
        try:
            self._require_owned(name, project, env_id)
            result = self.runner(
                exec_argv(name, ["/bin/sh", "-c", GUEST_CLEANUP, "ml-cleanup",
                                 str(guest.pid), mode],
                          workdir=None, env={}, user=user),
                capture=True, timeout=timeout)
        except (UnifierError, OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
        if result.returncode != 0 or (result.stdout or "").strip() != "CLEANED":
            return "unknown"
        return self.probe_guest(name, guest, project=project, env_id=env_id,
                                user=user)

    def _require_owned(self, name: str, project: str, env_id: str) -> ContainerState:
        info = self.inspect(name)
        if info is None:
            raise UnifierError(f"container {name} does not exist")
        if info.project != project or info.env_id != env_id or info.role != ROLE_ENVIRONMENT:
            raise UnifierError(
                f"refusing to touch {name}: it is not environment {env_id} of this project")
        return info

    def _check(self, argv: list[str], *, timeout: int, action: str) -> None:
        result = self.runner(argv, capture=True, timeout=timeout)
        if result.returncode:
            detail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
            raise UnifierError(f"podman {action} failed: {detail}")

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
        raise UnifierError(f"{argv[0]} failed: {exc}") from exc
    if capture:
        result.stdout = decode_output(result.stdout)
        result.stderr = decode_output(result.stderr)
    return result
