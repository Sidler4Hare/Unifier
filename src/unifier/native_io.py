"""Native package output, with an actual terminal for native prompts.

The PTY transcript intentionally merges stdout/stderr, just as a terminal
does. Pipe mode keeps the two streams separate and never consumes stdin.
"""
from __future__ import annotations

import errno
import contextlib
import fcntl
import os
import pty
import selectors
import signal
import subprocess
import sys
import termios
import time
import threading
import tty

from unifier.errors import UnifierError


class NativeCommandError(UnifierError):
    """An interrupted command, including output and verified cleanup outcome."""

    def __init__(self, message, *, stdout="", stderr="", returncode=None,
                 cleanup="unknown", signum=None):
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr
        self.returncode, self.cleanup, self.signum = returncode, cleanup, signum


class _NativeSignal(Exception):
    def __init__(self, signum):
        self.signum = signum


def terminal_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def add_package_arguments(parser, *, repair=False, json_option=True):
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interactive", action="store_true",
                      help="keep native package prompts and terminal input")
    mode.add_argument("-y", "--yes", action="store_true",
                      help="use native noninteractive package mode")
    if json_option:
        parser.add_argument("--json", action="store_true", help="JSON result without native progress output")
    if repair:
        parser.add_argument("--no-repair-deps", action="store_true",
                            help="skip the additional undeclared ELF dependency check")


def package_options(args, *, repair=False) -> dict:
    explicit = bool(getattr(args, "interactive", False))
    json_mode = bool(getattr(args, "json", False))
    if explicit and json_mode:
        raise UnifierError("--interactive and --json cannot be combined")
    if explicit and not terminal_available():
        raise UnifierError("--interactive requires a terminal on stdin and stdout")
    options = {"interactive": explicit or (terminal_available() and not json_mode
                                           and not getattr(args, "yes", False)),
               "stream": not json_mode}
    if repair:
        options["repair_dependencies"] = not getattr(args, "no_repair_deps", False)
    return options


def _write(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def _process_group_active(group: int) -> bool | None:
    """Check live members, including children whose group leader has exited.

    A zombie retains its process group until its parent reaps it, but cannot
    continue a command or hold its output pipes open. Unreadable process state
    is not evidence that cleanup succeeded.
    """
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    unknown = False
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    with open(f"/proc/{entry.name}/stat") as source:
                        fields = source.read().rsplit(") ", 1)[1].split()
                    if int(fields[2]) == group and fields[0] != "Z":
                        return True
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, ValueError, IndexError):
                    unknown = True
    except OSError:
        return None
    return None if unknown else False


def _terminate_process_group(process) -> str:
    # The leader can already be reaped while its descendants still hold the
    # pipes. Always address the group we created, not just a running leader.
    for number, grace in ((signal.SIGTERM, 1), (signal.SIGKILL, 1)):
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            break
        except OSError:
            return "unknown"
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if _process_group_active(process.pid) is False:
                break
            time.sleep(.01)
        else:
            continue
        break
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return "unknown"
    return "gone" if _process_group_active(process.pid) is False else "unknown"


def run_native(argv: list[str], *, interactive: bool, stream: bool,
               timeout: int | None, spawn=subprocess.Popen, abort=None) -> subprocess.CompletedProcess:
    if interactive and not terminal_available():
        raise UnifierError("native interactive mode requires a terminal on stdin and stdout")
    master = slave = None
    previous = resize_handler = None
    process = None
    output = bytearray()
    errors = bytearray()
    selector = selectors.DefaultSelector()
    handlers = {}
    failure = None
    cleanup = "unknown"
    signum = None

    def interrupted(number, _frame):
        raise _NativeSignal(number)

    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                handlers[number] = signal.signal(number, interrupted)
        if interactive:
            master, slave = pty.openpty()

            def resize(*_):
                size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(master, termios.TIOCSWINSZ, size)
                if process is not None and process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(process.pid, signal.SIGWINCH)

            resize()
            process = spawn(argv, stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True)
            os.close(slave)
            slave = None
            previous = termios.tcgetattr(sys.stdin.fileno())
            resize_handler = signal.signal(signal.SIGWINCH, resize)
            tty.setraw(sys.stdin.fileno())
            selector.register(master, selectors.EVENT_READ, (output, 1))
            selector.register(sys.stdin.fileno(), selectors.EVENT_READ, None)
        else:
            process = spawn(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
            selector.register(process.stdout, selectors.EVENT_READ, (output, 1))
            selector.register(process.stderr, selectors.EVENT_READ, (errors, 2))
        deadline = time.monotonic() + timeout if timeout is not None else None
        while any(key.data is not None for key in selector.get_map().values()):
            if deadline is not None and time.monotonic() >= deadline:
                raise UnifierError("native package command timed out; inspect package state before retrying")
            for key, _ in selector.select(0.1):
                fd = key.fd
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data is None:
                    _write(master, data)
                else:
                    buffer, destination = key.data
                    buffer.extend(data)
                    if stream or interactive:
                        _write(destination, data)
        code = process.wait(timeout=max(0.1, deadline - time.monotonic()) if deadline else None)
        return subprocess.CompletedProcess(argv, code, output.decode("utf-8", "replace"),
                                           errors.decode("utf-8", "replace"))
    except (Exception, KeyboardInterrupt) as exc:
        failure = exc
        signum = exc.signum if isinstance(exc, _NativeSignal) else (
            signal.SIGINT if isinstance(exc, KeyboardInterrupt) else None)
    finally:
        # Repeated cancellation must not interrupt guest cleanup or leave the
        # caller's terminal raw. The original handlers are always restored.
        for number in handlers:
            signal.signal(number, signal.SIG_IGN)
        try:
            if failure is not None and abort is not None:
                try:
                    cleanup = abort()
                except Exception as exc:
                    failure = UnifierError(f"{failure}; guest cleanup failed: {exc}")
            local_cleanup = "gone"
            if process is not None and (failure is not None or process.poll() is None):
                local_cleanup = _terminate_process_group(process)
            if failure is not None and abort is None:
                cleanup = local_cleanup
        finally:
            selector.close()
            if previous is not None:
                with contextlib.suppress(OSError, termios.error):
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
                signal.signal(signal.SIGWINCH, resize_handler)
            for number, handler in handlers.items():
                signal.signal(number, handler)
            if process is not None:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)
    message = (f"native package command interrupted by {signal.Signals(signum).name}"
               if signum else str(failure))
    raise NativeCommandError(message, stdout=output.decode("utf-8", "replace"),
                             stderr=errors.decode("utf-8", "replace"),
                             returncode=process.returncode if process else None,
                             cleanup=cleanup, signum=signum) from failure
