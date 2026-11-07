"""Host-side exec helper that puts the guest control pipe on FD 3.

The guest wrapper writes its execution identity (pid, start time, process group
and session) over one dedicated descriptor so stdout and stderr stay separate.
Real hosts only give one portable way to carry that descriptor into a
container: Podman on Ubuntu 24.04 (runc) documents only ``--preserve-fds N``,
which passes host descriptors 3..N+2 into the container with the same numbers.
The singular ``--preserve-fd`` of newer Podman is simply an unknown flag there,
so a native package transaction dies before DNF ever runs.  The only portable
shape is therefore ``--preserve-fds 1`` with the control descriptor sitting on
host FD 3, which every supported runtime (runc and crun alike) honours.

The caller owns the pipe, so the descriptor has to be moved to 3 before Podman
starts.  Doing that in the caller means dup2() inside a possibly multi-threaded
supervisor right between fork and exec, which is not safe, and ``preexec_fn``
has the same problem while also being unsafe with threads.  This module does the
move in a child that was already forked with ``close_fds`` finished: it takes
exactly one inherited descriptor, remaps it to 3, closes the original number and
execs the requested program.  Because it execs, the caller's proxy keeps one
process: its exit status and delivered signals still describe the real program.

The helper is deliberately import-free besides the standard library so it can be
run by path from any worker, with or without a PYTHONPATH that reaches this
package.  It is used as ``python3 /abs/path/exec_control.py <fd> -- <argv...>``
(or ``python3 -m unifier.exec_control`` when the package is importable).
"""

import os
import sys

# The one guest descriptor the wrapper protocol uses.  ``--preserve-fds N``
# numbers the carried descriptors 3..N+2, so 3 is the only fixed point.
CONTROL_GUEST_FD = 3

# Paths are resolved from this file so the helper works no matter how the
# unifier package was installed (source tree, wheel, venv script).
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "exec_control.py")

USAGE = "usage: exec_control.py <control_fd> -- <program> [arg ...]"

# 125 is what the guest wrapper already uses for "a prerequisite failed", so a
# helper that never reached the program is not mistaken for its exit status.
HELPER_ERROR = 125


def control_exec_argv(control_fd, command, *, interpreter=None):
    """Return the argv that runs ``command`` with ``control_fd`` moved to FD 3.

    ``control_fd`` is the caller's still-open write end of the control pipe.  It
    must be passed to the child with ``pass_fds=(control_fd,)``; nothing else is
    inherited, so the guest never sees unrelated descriptors.
    """
    if isinstance(control_fd, bool) or not isinstance(control_fd, int) \
            or control_fd < CONTROL_GUEST_FD:
        raise ValueError("control descriptor must be an integer >= 3")
    command = list(command)
    if not command:
        raise ValueError("exec helper needs a program to exec")
    if not isinstance(command[0], str) or not command[0]:
        raise ValueError("exec helper needs a non-empty program")
    for item in command:
        # execve permits empty argument values. The APP protocol deliberately
        # uses one for its default receipt root; never drop or rewrite it.
        if not isinstance(item, str) or '\0' in item:
            raise ValueError("exec helper argv must be NUL-free strings")
    return [interpreter or (sys.executable or "python3"), SCRIPT_PATH,
            str(control_fd), "--", *command]


def _fail(detail):
    """Report a setup failure without pretending the program ran."""
    sys.stderr.write(f"exec-control: {detail}\n")
    sys.stderr.flush()
    return HELPER_ERROR


def remap_control_fd(control_fd):
    """Move ``control_fd`` onto 3 and close the original number.

    Only called after ``close_fds`` has run, so 0/1/2 plus the single inherited
    descriptor are all that exist.  An occupied FD 3 would silently shadow a
    stream, so the helper refuses instead of guessing which descriptor Podman
    would carry into the guest.
    """
    try:
        os.fstat(control_fd)
    except OSError as exc:
        raise OSError(f"control descriptor {control_fd} is not open ({exc.strerror})")
    if control_fd != CONTROL_GUEST_FD:
        try:
            os.fstat(CONTROL_GUEST_FD)
        except OSError:
            pass
        else:
            raise OSError(f"descriptor {CONTROL_GUEST_FD} is already open and is not "
                          f"the control descriptor {control_fd}")
    # inheritable=True clears FD_CLOEXEC: execvp must keep it for Podman.
    os.dup2(control_fd, CONTROL_GUEST_FD, inheritable=True)
    if control_fd != CONTROL_GUEST_FD:
        try:
            os.close(control_fd)
        except OSError:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3:
        sys.stderr.write(USAGE + "\n")
        return HELPER_ERROR
    if argv[1] != "--" or not argv[2]:
        return _fail("expected '<control_fd> -- <program> ...'")
    try:
        control_fd = int(argv[0], 10)
    except ValueError:
        return _fail(f"control descriptor {argv[0]!r} is not an integer")
    if control_fd < CONTROL_GUEST_FD:
        return _fail("control descriptor must not steal a stdio slot")
    try:
        remap_control_fd(control_fd)
    except OSError as exc:
        return _fail(str(exc))
    program = argv[2:]
    try:
        os.execvp(program[0], program)
    except FileNotFoundError:
        return _fail(f"program not found: {program[0]}")
    except PermissionError:
        return _fail(f"program not executable: {program[0]}")
    except OSError as exc:
        return _fail(f"cannot exec {program[0]}: {exc}")
    return HELPER_ERROR  # execvp only returns on failure


if __name__ == "__main__":
    raise SystemExit(main())
