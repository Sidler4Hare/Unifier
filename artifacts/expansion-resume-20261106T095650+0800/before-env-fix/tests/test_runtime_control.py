"""Cross-host control-channel regressions for the guest execution handshake.

Real evidence from an Ubuntu 24.04 server: ``podman exec --help`` only offers
``--preserve-fds N`` (runc).  The singular ``--preserve-fd`` that a newer, crun
host accepts is an unknown flag there, so a native package transaction died with
"guest execution exited before reporting its identity" before DNF had run.  These
tests pin the portable replacement: exactly ``--preserve-fds 1``; a host exec
helper that moves the single inherited control descriptor onto FD 3 and execs
podman; and a real handshake that still reports a real pid/start/pgrp/sid over
that descriptor instead of over stdout.
"""

import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from unifier import exec_control
from unifier.errors import UnifierError
from unifier.exec_control import CONTROL_GUEST_FD, SCRIPT_PATH, control_exec_argv
from unifier.runtime import (CONTROL_FD, GUEST_SETSID_BOOTSTRAP, GUEST_WRAPPER,
                             LABEL_ENV, LABEL_PROJECT, LABEL_ROLE, ROLE_ENVIRONMENT,
                             PodmanRuntime, exec_argv, guest_session_argv,
                             host_exec_command)

# What a guest writes on the carried descriptor; the fake client below emits
# exactly these bytes on FD 3.
CONTROL_PAYLOAD = b"CONTROL-PAYLOAD\n"

PROJECT = "/srv/project"
ENV_ID = "arch"
CONTAINER = "ml-demo"
CONTAINER_ID = "cafe1234"

# Stands in for ``podman exec`` on a host whose client really honours
# --preserve-fds 1: it rejects the flag shape an old runc client rejects,
# refuses anything but one preserved descriptor, insists that the preserved
# descriptor actually is FD 3, records which descriptors it was given, and only
# then execs the guest command with FD 3 still inheritable.
FAKE_PODMAN = r'''#!/usr/bin/env python3
import os
import signal
import sys
import time


def report(lines):
    path = os.environ.get("FAKE_REPORT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def refuse(status, detail):
    sys.stderr.write(detail + "\n")
    sys.stderr.flush()
    raise SystemExit(status)


argv = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "exec")
if argv[:1] != ["exec"]:
    refuse(126, "fake-podman: expected an exec, got %r" % (argv[:3],))
# A 4.x/runc client stops right here on the singular flag.
if any(item == "--preserve-fd" or item.startswith("--preserve-fd=") for item in argv):
    refuse(125, "Error: unknown flag: --preserve-fd")
if argv.count("--preserve-fds") != 1:
    refuse(125, "fake-podman: expected exactly one --preserve-fds")
if argv[argv.index("--preserve-fds") + 1] != "1":
    refuse(125, "fake-podman: only --preserve-fds 1 is carried, got "
                + argv[argv.index("--preserve-fds") + 1])
# --preserve-fds N means host descriptors 3..N+2 exist and keep their numbers.
def descriptors():
    """(number, target) for every descriptor this client was handed.

    The listing's own fd is skipped: it is an artefact of looking, not something
    podman would carry.  Every other entry keeps its target, so a test can tell
    the interpreter's own script file apart from a leaked pipe or file.
    """
    entries = []
    for name in os.listdir("/proc/self/fd"):
        if not name.isdigit():
            continue
        number = int(name)
        try:
            os.fstat(number)
            target = os.readlink("/proc/self/fd/%d" % number)
        except OSError:
            continue
        if target == "/proc/self/fd":
            continue
        entries.append((number, target))
    return entries


entries = descriptors()
alive = set(number for number, _ in entries)
report(["MODE=" + mode,
        "OPEN3=%s" % (3 in alive),
        "EXTRA=" + ",".join("%d:%s" % pair for pair in sorted(entries) if pair[0] > 3),
        "ARGV=" + " ".join(argv).replace("\n", "|")])
if 3 not in alive:
    refuse(125, "Error: file descriptor 3 is not open")
sentinel = os.environ.get("FAKE_SENTINEL_FD")
if sentinel:
    report(["SENTINEL=%s=%s" % (sentinel,
                                "open" if int(sentinel) in alive else "closed")])
if mode == "reject":
    # Structurally accepted, then dead before any guest command ever runs.
    refuse(125, "Error: unrecognized --preserve-fd flag")
if mode == "hang":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "30")))
    raise SystemExit(0)
if mode == "control":
    # Behave like the guest wrapper: the identity goes to FD 3 and nowhere else,
    # while stdout/stderr stay reserved for the command's own output.
    try:
        # Must stay exactly CONTROL_PAYLOAD from the test module.
        note = "STDOUT-OK written=%d\n" % os.write(3, b"CONTROL-PAYLOAD\n")
    except OSError as exc:
        note = "STDOUT-ERROR %s\n" % exc
    sys.stdout.write(note)
    sys.stdout.flush()
    sys.stderr.write("STDERR-OK\n")
    sys.stderr.flush()
    raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
if mode == "ready-signal":
    os.write(3, b"READY\n")
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(30)
    raise SystemExit(0)

index = 1
guest = None
while index < len(argv):
    item = argv[index]
    if item in ("-i", "-t"):
        index += 1
    elif item in ("--user", "--workdir", "--env", "--preserve-fds"):
        index += 2
    elif item.startswith("--"):
        index += 1
    else:
        guest = argv[index + 1:]
        break
if not guest:
    refuse(126, "fake-podman: no guest command after the container id")
os.execvp(guest[0], guest)
refuse(127, "fake-podman: cannot exec %r" % (guest,))
'''


def proc_start(pid: int) -> int:
    """Boot-relative start time of a live process: the anti-recycling token."""
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def guest_control_argument(argv) -> str:
    """The control-fd argument the fixed guest wrapper is being handed.

    The TTY form is ``sh -c <wrapper> ml-session <token> <fd>`` and the non-TTY
    form wraps it in ``sh -c <setsid bootstrap> ml-session <wrapper> <token>
    <fd>``, so the position depends on which form an argv uses.
    """
    marker = argv.index("ml-session")
    before = argv[marker - 1]
    if before == GUEST_WRAPPER:
        return argv[marker + 2]
    if before == GUEST_SETSID_BOOTSTRAP:
        return argv[marker + 3]
    raise AssertionError(f"unrecognised guest wrapper form: {before!r}")


def open_fds() -> set:
    """This process's open descriptors, without the listing itself counting."""
    found = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            number = int(name)
            os.fstat(number)
        except (OSError, ValueError):
            continue
        found.add(number)
    return found


def preserved_flags(argv) -> list:
    """Every preserve request in an argv, as (flag, value) pairs."""
    pairs = []
    for index, item in enumerate(argv):
        if item in ("--preserve-fds", "--preserve-fd"):
            pairs.append((item, argv[index + 1] if index + 1 < len(argv) else None))
        elif item.startswith("--preserve-fd"):
            flag, _, value = item.partition("=")
            pairs.append((flag, value))
    return pairs


def extra_descriptors(report: str) -> list:
    """The (number, target) pairs a fake client recorded above FD 3."""
    lines = [line for line in report.splitlines() if line.startswith("EXTRA=")]
    if not lines:
        raise AssertionError(f"the fake client recorded no descriptor audit: {report!r}")
    pairs = []
    for entry in lines[0][len("EXTRA="):].split(","):
        if not entry:
            continue
        number, _, target = entry.partition(":")
        pairs.append((int(number), target))
    return pairs


def high_numbered_open(path) -> int:
    """Open ``path`` at a descriptor number far above 3, never a stdio slot."""
    padding = [os.open(os.devnull, os.O_RDONLY) for _ in range(60)]
    try:
        descriptor = os.open(path, os.O_RDONLY)
    finally:
        for number in padding:
            os.close(number)
    if descriptor <= 30:
        os.close(descriptor)
        raise AssertionError(f"could not open {path} far above FD 3 (got {descriptor})")
    return descriptor


@contextlib.contextmanager
def caller_fd3_available():
    """Give a single-threaded test process a free FD 3, then put back what was there.

    Production deliberately never does this: a dup2 onto FD 3 in the calling
    supervisor would land between another thread's fork and exec.  A test may, as
    long as it restores the descriptor afterwards.
    """
    saved = os.dup(3) if 3 in open_fds() else None
    saved_inheritable = os.get_inheritable(3) if saved is not None else False
    if saved is not None:
        os.close(3)
    try:
        yield
    finally:
        try:
            os.close(3)
        except OSError:
            pass
        if saved is not None:
            os.dup2(saved, 3)
            os.set_inheritable(3, saved_inheritable)
            os.close(saved)


def high_numbered_pipe():
    """A control pipe whose write end sits far above 3, like a busy supervisor."""
    padding = [os.open(os.devnull, os.O_RDONLY) for _ in range(60)]
    try:
        read_fd, write_fd = os.pipe()
    finally:
        for descriptor in padding:
            os.close(descriptor)
    if write_fd <= 30:
        os.close(read_fd)
        os.close(write_fd)
        raise AssertionError(f"could not build a control pipe far above FD 3 (got {write_fd})")
    return read_fd, write_fd


class ControlArgumentShape(unittest.TestCase):
    """The exec argv must be the one shape every supported host accepts."""

    base = dict(workdir="/work", env={"LANG": "C"})

    def test_session_environment_can_reference_names_without_exposing_values(self):
        command = exec_argv(CONTAINER, ["/bin/true"], workdir=None,
                            env={"UNIFIER_AGENT_TOKEN": "private-test-credential"},
                            env_from_process=True)
        self.assertIn("UNIFIER_AGENT_TOKEN", command)
        self.assertNotIn("private-test-credential", " ".join(command))

    def test_control_channel_is_preserve_fds_1_and_never_preserve_fd(self):
        command = exec_argv(CONTAINER, ["/bin/sh", "-c", "true"],
                            interactive=True, control_fd=CONTROL_FD, **self.base)
        self.assertEqual(preserved_flags(command), [("--preserve-fds", "1")])
        self.assertNotIn("--preserve-fd", command)
        self.assertNotIn("--preserve-fd=1", command)
        self.assertNotIn("--preserve-fd=3", command)
        # Exactly one descriptor crosses the host/guest boundary: nothing below
        # FD 3 and no range 3..N.
        self.assertEqual(command[command.index("--preserve-fds"):][:2],
                         ["--preserve-fds", "1"])
        self.assertEqual(command[-4:], [CONTAINER, "/bin/sh", "-c", "true"])

    def test_tty_and_user_variants_still_only_ask_for_one_descriptor(self):
        for options in ({"tty": True, "interactive": True},
                        {"user": "0", "interactive": True},
                        {"user": "1000:1000", "interactive": True, "tty": True}):
            command = exec_argv(CONTAINER, ["/usr/bin/dnf", "-y", "install"],
                                control_fd=CONTROL_FD, workdir=None, env={},
                                **options)
            with self.subTest(options=options):
                self.assertEqual(preserved_flags(command), [("--preserve-fds", "1")],
                                 "the control channel degraded per runtime")
                self.assertNotIn("--preserve-fd", command)
                self.assertNotIn("--preserve-fd=1", command)

    def test_control_free_execs_ask_for_no_preserved_descriptor(self):
        # probe/signal/cleanup talk on their own stdout; they must inherit nothing.
        for options in ({}, {"user": "0"}, {"interactive": True}):
            command = exec_argv(CONTAINER, ["/bin/sh", "-c", "true"],
                                workdir=None, env={}, **options)
            with self.subTest(options=options):
                self.assertEqual(preserved_flags(command), [])
                self.assertNotIn("--preserve-fds", command)

    def test_a_non_portable_control_descriptor_fails_closed(self):
        for wrong in (4, 7, 2, -3, True, "3", 3.0):
            with self.subTest(control_fd=wrong):
                with self.assertRaises(UnifierError):
                    exec_argv(CONTAINER, ["/bin/true"], workdir=None, env={},
                              control_fd=wrong)

    def test_guest_wrapper_is_told_to_use_the_container_side_fd_3(self):
        self.assertEqual(CONTROL_FD, CONTROL_GUEST_FD)
        token = "0123456789abcdef" * 2
        for tty in (False, True):
            argv = guest_session_argv(["/bin/sh", "-c", "true"], token=token,
                                      control_fd=CONTROL_FD, tty=tty)
            with self.subTest(tty=tty):
                self.assertEqual(guest_control_argument(argv), "3")
                self.assertNotEqual(guest_control_argument(argv), "124")

    def test_host_helper_wraps_the_exec_without_a_pythonpath_dependency(self):
        command = host_exec_command(124, exec_argv(
            CONTAINER, ["/bin/true"], workdir=None, env={}, control_fd=CONTROL_FD))
        self.assertEqual(command[0], sys.executable or "python3")
        self.assertEqual(command[1], SCRIPT_PATH)
        self.assertTrue(os.path.isfile(SCRIPT_PATH),
                        "the helper must be reachable by path, not by PYTHONPATH")
        self.assertEqual(command[2:4], ["124", "--"])
        self.assertEqual(command[4:6], ["podman", "exec"])
        self.assertEqual(preserved_flags(command), [("--preserve-fds", "1")])
        self.assertNotIn("--preserve-fd", command)
        with self.assertRaises(UnifierError):
            host_exec_command(2, ["podman", "exec"])
        with self.assertRaises(UnifierError):
            host_exec_command(4, [])


@unittest.skipUnless(Path("/proc/self/fd").is_dir(), "the fake client audits /proc/self/fd")
class HostExecHelper(unittest.TestCase):
    """Run the real helper as a real subprocess, with no container involved."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="unifier-exec-helper-"))
        cls.shim = cls.root / "fake-podman"
        cls.shim.write_text(FAKE_PODMAN, encoding="utf-8")
        os.chmod(cls.shim, 0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.report = self.root / f"report-{self.id().rsplit('.', 1)[-1]}"
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def spawn(self, write_fd, mode, *, exit_code=None, sentinel=None, env=None):
        """Hand the helper one inherited pipe end plus the fake client argv."""
        command = control_exec_argv(
            write_fd, [str(self.shim), "exec", "--preserve-fds", "1", "--user",
                       "0", CONTAINER, "/bin/true"])
        environment = os.environ.copy()
        environment["PATH"] = os.environ.get("PATH", "")
        environment["FAKE_MODE"] = mode
        environment["FAKE_REPORT"] = str(self.report)
        if exit_code is not None:
            environment["FAKE_EXIT"] = str(exit_code)
        if sentinel is not None:
            environment["FAKE_SENTINEL_FD"] = str(sentinel)
        if env:
            environment.update(env)
        process = subprocess.Popen(command, env=environment, pass_fds=(write_fd,),
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        self.processes.append(process)
        return process

    def read_report(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.report.is_file() and "ARGV=" in self.report.read_text():
                return self.report.read_text()
            time.sleep(0.02)
        self.fail(f"the fake client never reported its view: {self.report}")

    def drain_control_pipe(self, read_fd) -> bytes:
        """Read the host's end of the control pipe until every writer is gone."""
        data = b""
        while len(data) < 4096:
            chunk = os.read(read_fd, 64)
            if not chunk:
                return data
            data += chunk
        raise AssertionError(f"unexpectedly large control payload: {len(data)} bytes")

    def assert_only_the_channel(self, report, pipe_fd=None):
        """Nothing but the control descriptor may reach the execed program."""
        self.assertIn("OPEN3=True", report)
        for number, target in extra_descriptors(report):
            self.assertGreater(number, 3)
            self.assertEqual(os.path.realpath(target), os.path.realpath(str(self.shim)),
                             f"descriptor {number}:{target} leaked into the exec")
        self.assertNotIn("pipe:[", report, "a pipe descriptor leaked into the exec")
        if pipe_fd is not None:
            self.assertNotIn(pipe_fd, [number for number, _ in extra_descriptors(report)],
                             "the original control pipe number stayed open")

    def test_only_fd_3_carries_control_data_when_the_pipe_fd_is_far_above_three(self):
        read_fd, pipe_fd = high_numbered_pipe()
        # An unrelated inheritable descriptor: close_fds must still drop it, and
        # --preserve-fds 1 must not drag every FD below N into the container.
        sentinel_path = self.root / "sentinel"
        sentinel_path.write_text("SENTINEL-PAYLOAD", encoding="utf-8")
        sentinel = high_numbered_open(sentinel_path)
        os.set_inheritable(sentinel, True)
        self.assertNotIn(sentinel, (read_fd, pipe_fd))
        try:
            process = self.spawn(pipe_fd, "control", sentinel=sentinel)
            # The parent's copy closes right after the spawn: FD 3, i.e. the one
            # descriptor the helper remapped, is the only writer left.
            os.close(pipe_fd)
            pipe_fd = None
            control = self.drain_control_pipe(read_fd)
        finally:
            for descriptor in (pipe_fd, read_fd, sentinel):
                if descriptor is not None:
                    os.close(descriptor)
        # The control payload travelled on FD 3 and nowhere else.
        self.assertEqual(control, CONTROL_PAYLOAD)
        out, err = process.communicate(timeout=30)
        self.assertEqual(out, b"STDOUT-OK written=%d\n" % len(CONTROL_PAYLOAD))
        self.assertEqual(err, b"STDERR-OK\n")
        self.assertNotIn(b"CONTROL-PAYLOAD", out + err)
        self.assertEqual(process.returncode, 0)
        report = self.read_report()
        self.assert_only_the_channel(report)
        self.assertEqual([line for line in report.splitlines()
                          if line.startswith(f"SENTINEL={sentinel}=")][0],
                         f"SENTINEL={sentinel}=closed")

    def test_a_caller_that_already_holds_the_pipe_on_fd_3_still_works(self):
        # os.pipe() lands on 3 whenever a process is quiet enough.  The helper
        # must then clear CLOEXEC without closing the very descriptor the caller
        # is standing on, and the channel must still be the only one inherited.
        read_fd, pipe_fd = high_numbered_pipe()
        with caller_fd3_available():
            os.dup2(pipe_fd, 3)
            os.close(pipe_fd)
            process = self.spawn(3, "control")
            os.close(3)
            control = self.drain_control_pipe(read_fd)
        os.close(read_fd)
        out, err = process.communicate(timeout=30)
        self.assertEqual(control, CONTROL_PAYLOAD)
        self.assertEqual(out, b"STDOUT-OK written=%d\n" % len(CONTROL_PAYLOAD))
        self.assertEqual(err, b"STDERR-OK\n")
        self.assertEqual(process.returncode, 0)
        self.assert_only_the_channel(self.read_report())

    def test_helper_exit_status_and_signal_describe_the_execed_program(self):
        # Exit status: 9 is the program's own, not a wrapper's translation.
        read_fd, pipe_fd = high_numbered_pipe()
        process = self.spawn(pipe_fd, "control", exit_code=9)
        os.close(pipe_fd)
        self.assertEqual(self.drain_control_pipe(read_fd), CONTROL_PAYLOAD)
        os.close(read_fd)
        out, err = process.communicate(timeout=30)
        self.assertEqual(out, b"STDOUT-OK written=%d\n" % len(CONTROL_PAYLOAD))
        self.assertEqual(err, b"STDERR-OK\n")
        self.assertEqual(process.returncode, 9)

        # Signals: the proxy's own pid must be the program.  The helper execs
        # instead of forking, so no wrapper absorbs or translates a signal.
        read_fd, pipe_fd = high_numbered_pipe()
        process = self.spawn(pipe_fd, "ready-signal")
        os.close(pipe_fd)
        self.assertEqual(os.read(read_fd, 16), b"READY\n")
        os.close(read_fd)
        self.assertEqual(process.wait(timeout=15), -signal.SIGTERM)
        self.assertEqual(process.poll(), -signal.SIGTERM)

    def test_helper_fails_closed_before_running_anything(self):
        # A descriptor that is not open can never become the control channel.
        command = control_exec_argv(900, [sys.executable, "-c", "print('RAN')"])
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=30, env=os.environ.copy())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("control descriptor 900", result.stderr)

        # Nor can a malformed request, and a bad request never runs the program.
        for bad in (["x", "--", "/bin/true"], ["1", "--", "/bin/true"],
                    ["4", "-c", "/bin/true"], ["4"], ["4", "--"]):
            with self.subTest(helper_argv=bad):
                result = subprocess.run(
                    [sys.executable, SCRIPT_PATH, *bad], capture_output=True,
                    text=True, timeout=30, env=os.environ.copy())
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_helper_runs_by_path_with_no_package_and_no_pythonpath(self):
        # A worker may carry any PYTHONPATH, so the helper is started by file
        # path and must import nothing from this package.
        read_fd, write_fd = high_numbered_pipe()
        command = control_exec_argv(write_fd, ["/bin/sh", "-c", "printf RAN"])
        environment = {"PATH": os.environ.get("PATH", "")}
        self.assertNotIn("PYTHONPATH", environment,
                         "the child must not lean on the worker's PYTHONPATH")
        process = subprocess.Popen(command, env=environment, pass_fds=(write_fd,),
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.close(write_fd)
        os.close(read_fd)
        out, err = process.communicate(timeout=30)
        self.assertEqual((process.returncode, out, err), (0, b"RAN", b""))

    def test_remap_refuses_to_shadow_or_lose_the_descriptor(self):
        for bad_fd in (2, True, 0, -1):
            with self.subTest(control_fd=bad_fd):
                with self.assertRaises(ValueError):
                    exec_control.control_exec_argv(bad_fd, ["/bin/true"])
        with self.assertRaises(ValueError):
            exec_control.control_exec_argv(4, [])
        with self.assertRaises(OSError):
            exec_control.remap_control_fd(900)

        read_fd, write_fd = high_numbered_pipe()
        saved = os.dup(3) if 3 in open_fds() else None
        saved_inheritable = os.get_inheritable(3) if saved is not None else False
        try:
            if saved is not None:
                os.close(3)
            exec_control.remap_control_fd(write_fd)
            self.assertIn(3, open_fds())
            self.assertTrue(os.get_inheritable(3),
                            "podman must keep the descriptor across its own exec")
            self.assertNotIn(write_fd, open_fds(), "the original number must close")
            os.write(3, b"X")
            self.assertEqual(os.read(read_fd, 2), b"X")
            # A second descriptor cannot silently shadow the control channel.
            second_read, second_write = os.pipe()
            try:
                with self.assertRaises(OSError):
                    exec_control.remap_control_fd(second_write)
            finally:
                os.close(second_read)
                os.close(second_write)
        finally:
            for descriptor in (3, read_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if write_fd in open_fds():
                os.close(write_fd)
            if saved is not None:
                os.dup2(saved, 3)
                os.set_inheritable(3, saved_inheritable)
                os.close(saved)


@unittest.skipUnless(shutil.which("setsid") and Path("/proc/self/stat").is_file(),
                     "the real handshake needs setsid and /proc")
@unittest.skipUnless(Path("/proc/self/fd").is_dir(), "the fake client audits /proc/self/fd")
class RealHandshake(unittest.TestCase):
    """Drive PodmanRuntime against a fake client that really carries FD 3."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="unifier-handshake-"))
        cls.shim = cls.root / "fake-podman"
        cls.shim.write_text(FAKE_PODMAN, encoding="utf-8")
        os.chmod(cls.shim, 0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.mode = "exec"
        self.extra_env = {}
        self.report = None
        self.last_command = None
        self.processes = []
        # check_rootless would ask a real podman for its facts.
        self.runtime = PodmanRuntime(runner=self._runner, popen=self._popen,
                                    check_rootless=False)

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _runner(self, argv, *, capture=True, timeout=None):
        """Answer inspect locally and run the guest shell protocol for real."""
        if argv[:3] == ["podman", "inspect", "--format"]:
            labels = {LABEL_PROJECT: PROJECT, LABEL_ENV: ENV_ID,
                      LABEL_ROLE: ROLE_ENVIRONMENT}
            payload = [{"Id": CONTAINER_ID, "ImageName": "image@sha256:x",
                        "Config": {"Labels": labels},
                        "State": {"Running": True, "Status": "running",
                                  "Pid": os.getpid()}}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if argv[:2] == ["podman", "exec"] and "-c" in argv:
            index = argv.index("-c")
            self.assertEqual(argv[index - 1], "/bin/sh")
            return subprocess.run(["/bin/sh"] + argv[index:], capture_output=True,
                                  text=True, timeout=timeout or 20)
        raise AssertionError(f"unexpected podman invocation: {argv}")

    def _popen(self, command, **kwargs):
        # The wrapped command is what podman must really see: only the program
        # name is swapped for the fake client, the helper stays in front of it.
        self.assertEqual(command[1], SCRIPT_PATH)
        self.assertEqual(command[3], "--")
        self.assertEqual(command[4], "podman")
        environment = dict(kwargs.pop("env", os.environ))
        environment.update(self.extra_env)
        environment["FAKE_MODE"] = self.mode
        if self.report is not None:
            environment["FAKE_REPORT"] = str(self.report)
        argv = list(command)
        argv[4] = str(self.shim)
        # A real caller inherits the supervisor's streams; a test reads them
        # instead of mixing guest output into unittest's own report.
        kwargs["stdin"] = subprocess.DEVNULL
        if kwargs.get("stdout") is None:
            kwargs["stdout"] = subprocess.PIPE
        if kwargs.get("stderr") is None:
            kwargs["stderr"] = subprocess.PIPE
        process = subprocess.Popen(argv, env=environment, **kwargs)
        self.last_command = list(command)
        self.processes.append(process)
        return process

    def assert_portable_channel(self, command):
        self.assertEqual(preserved_flags(command), [("--preserve-fds", "1")])
        self.assertNotIn("--preserve-fd", command)
        self.assertEqual(guest_control_argument(command), "3",
                         "the guest wrapper must be pointed at the carried descriptor")

    def read_report(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.report.is_file() and "ARGV=" in self.report.read_text():
                return self.report.read_text()
            time.sleep(0.02)
        self.fail(f"the fake client never reported its view: {self.report}")

    def assert_only_the_channel(self, report, command=None):
        """The fake client must have been handed the control FD and nothing more."""
        self.assertIn("OPEN3=True", report)
        audit = extra_descriptors(report)
        for number, target in audit:
            self.assertGreater(number, 3)
            self.assertEqual(os.path.realpath(target), os.path.realpath(str(self.shim)),
                             f"descriptor {number}:{target} leaked into the exec")
        self.assertNotIn("pipe:[", report, "the control pipe leaked into the exec")
        if command is not None:
            # command[2] is the host write end the caller passed with pass_fds.
            self.assertNotIn(int(command[2]), [number for number, _ in audit],
                             "the original control pipe number stayed open")

    def reap_proxy(self, process) -> None:
        """Close the streams the test attached, so a descriptor check is honest."""
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def test_private_session_environment_reaches_guest_without_argv_or_parent_mutation(self):
        key = "UNIFIER_TEST_PRIVATE_ENV"
        before = os.environ.get(key)
        value = "literal private $; value"
        execution = self.runtime.start_session_exec(
            CONTAINER, ["/bin/sh", "-c", 'printf %s "$UNIFIER_TEST_PRIVATE_ENV"'],
            project=PROJECT, env_id=ENV_ID, workdir=None, env={key: value},
            user="0", interactive=True, tty=False)
        output, error = execution.process.communicate(timeout=20)
        self.assertEqual(output, value.encode())
        self.assertEqual(error, b"")
        self.assertNotIn(value, " ".join(self.last_command))
        self.assertIn(key, self.last_command)
        self.assertEqual(os.environ.get(key), before)

    def test_session_handshake_reports_a_real_identity_over_fd_3(self):
        self.report = self.root / "report-session"
        spawned = []
        execution = self.runtime.start_session_exec(
            CONTAINER, ["/bin/sh", "-c", "printf OUT; printf ERR >&2; sleep 30"],
            project=PROJECT, env_id=ENV_ID, workdir="/work", env={"LANG": "C"},
            user="0", interactive=True, tty=False, on_spawn=spawned.append)
        self.addCleanup(execution.abort_proxy)
        self.assert_portable_channel(self.last_command)
        guest = execution.guest
        self.assertGreater(guest.pid, 1)
        self.assertGreater(guest.start, 0)
        self.assertEqual(len(guest.token), 32)
        # The identity is a live process, and its start time is what /proc still
        # carries for it, so a later cancel can never hit a recycled pid.
        self.assertEqual(proc_start(guest.pid), guest.start)
        self.assertEqual(guest.pgrp, guest.pid)
        self.assertEqual(guest.sid, guest.pid)
        self.assert_only_the_channel(self.read_report(), self.last_command)
        # Cancellation still targets that exact session and nothing else.
        self.assertEqual(self.runtime.signal_guest(CONTAINER, guest, signal.SIGTERM,
                                                   project=PROJECT, env_id=ENV_ID,
                                                   user="0"), "signalled")
        execution.process.communicate(timeout=20)
        self.assertNotEqual(execution.process.poll(), 0)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.runtime.probe_guest(CONTAINER, guest, project=PROJECT,
                                        env_id=ENV_ID, user="0") == "gone":
                break
            time.sleep(0.05)
        else:
            self.fail("the cancelled guest execution is still reported alive")

    def test_streams_stay_separate_and_ready_never_reaches_stdout(self):
        self.report = self.root / "report-streams"
        execution = self.runtime.start_session_exec(
            CONTAINER, ["/bin/sh", "-c", "printf OUT; printf ERR >&2"],
            project=PROJECT, env_id=ENV_ID, workdir=None, env={}, user="0",
            interactive=True, tty=False)
        out, err = execution.process.communicate(timeout=20)
        self.assertEqual(out, b"OUT")
        self.assertEqual(err, b"ERR")
        text = (out + err).decode("utf-8", "replace")
        self.assertNotIn("READY", text)
        self.assertNotIn(execution.guest.token, text)
        self.assertEqual(execution.poll(), 0)

    def test_the_runtime_never_remaps_a_descriptor_in_the_calling_process(self):
        # dup2 onto FD 3 in the supervisor itself would race another thread
        # between its fork and exec, so the remap belongs to the exec helper.
        marker = self.root / "caller-fd3-marker"
        marker.write_text("mine\n", encoding="utf-8")
        with caller_fd3_available():
            self.assertEqual(os.open(marker, os.O_RDONLY), 3,
                             "the marker has to sit on FD 3 for this to mean anything")
            execution = self.runtime.start_session_exec(
                CONTAINER, ["/bin/sh", "-c", "printf OUT"], project=PROJECT,
                env_id=ENV_ID, workdir=None, env={}, user="0", interactive=True,
                tty=False)
            self.assert_portable_channel(self.last_command)
            self.assertEqual(os.path.realpath(os.readlink("/proc/self/fd/3")),
                             os.path.realpath(marker),
                             "the caller's FD 3 was remapped by the runtime")
            out, err = execution.process.communicate(timeout=20)
            self.assertEqual((out, err), (b"OUT", b""))
            self.assertEqual(os.path.realpath(os.readlink("/proc/self/fd/3")),
                             os.path.realpath(marker),
                             "the control pipe leaked onto the caller's FD 3")

    def test_package_transaction_reaches_the_guest_with_the_same_channel(self):
        # This is the native package path that died on the runc host.
        self.report = self.root / "report-package"
        guests = []
        result = self.runtime.package_exec(
            CONTAINER, ["/bin/sh", "-c", "printf OUT; printf ERR >&2"],
            project=PROJECT, env_id=ENV_ID, env={"LANG": "C"}, interactive=False,
            stream=False, timeout=30, on_guest=guests.append)
        self.assert_portable_channel(self.last_command)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "OUT", "ERR"))
        self.assertNotIn("READY", result.stdout + result.stderr)
        self.assertEqual(len(guests), 1)
        guest = guests[0]
        self.assertGreater(guest.pid, 1)
        self.assertEqual((guest.pgrp, guest.sid), (guest.pid, guest.pid))
        self.assertEqual(len(guest.token), 32)
        self.assertIn("OPEN3=True", self.read_report())
        self.assert_only_the_channel(self.read_report(), self.last_command)

    def test_handshake_refusal_fails_closed_and_clears_the_proxy(self):
        self.mode = "reject"
        self.report = self.root / "report-reject"
        spawned = []
        before = open_fds()
        with self.assertRaises(UnifierError) as caught:
            self.runtime.start_session_exec(
                CONTAINER, ["/bin/true"], project=PROJECT, env_id=ENV_ID,
                workdir=None, env={}, user="0", interactive=True, tty=False,
                control_timeout=10.0, on_spawn=spawned.append)
        self.assertIn("exited before reporting its identity", str(caught.exception))
        process = spawned[0]
        self.assertIsNotNone(process.poll())
        self.assertNotEqual(process.poll(), 0)
        # The exec was accepted structurally and still died before the guest
        # spoke: that is a missing identity, never a claimed cancellation.
        self.assertIn("OPEN3=True", self.read_report())
        self.reap_proxy(process)
        self.assertEqual(open_fds() - before, set(), "the control pipe leaked")

    def test_handshake_timeout_kills_the_proxy_group(self):
        self.mode = "hang"
        self.extra_env = {"FAKE_SLEEP": "30"}
        self.report = self.root / "report-hang"
        spawned = []
        before = open_fds()
        with self.assertRaises(UnifierError) as caught:
            self.runtime.start_session_exec(
                CONTAINER, ["/bin/true"], project=PROJECT, env_id=ENV_ID,
                workdir=None, env={}, user="0", interactive=True, tty=False,
                control_timeout=0.4, on_spawn=spawned.append)
        self.assertIn("timed out before readiness", str(caught.exception))
        self.assertEqual(spawned[0].poll(), -signal.SIGKILL)
        self.reap_proxy(spawned[0])
        self.assertEqual(open_fds() - before, set(), "the control pipe leaked")


if __name__ == "__main__":
    unittest.main()
