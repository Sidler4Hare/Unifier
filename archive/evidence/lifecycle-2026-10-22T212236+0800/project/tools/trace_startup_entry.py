#!/usr/bin/env python3
"""Diagnostic wrapper: split a frozen multilinux CLI run into Podman spans.

Only metadata is written. Product stdout/stderr are inherited untouched, and
no argv, environment, credentials, or application output is recorded.
"""
from pathlib import Path
import hashlib, json, os, subprocess, sys, time

USAGE = ("usage: trace_startup_entry.py --project ABS_PROJECT --output ABS_JSON "
         "CLI_ARG [CLI_ARG ...]")
_TOP = set("build commit container cp create exec export generate healthcheck image import info init inspect kill load login logout logs machine manifest mount network pause pod port ps pull push rename restart rm rmi run save search secret start stats stop system tag top unmount unpause version volume wait".split())
_GROUPS = set("container generate healthcheck image kube machine manifest network pod secret system volume".split())
_VERBS = set("add attach build commit connect create diff disconnect exec exists export history import info init inspect kill list load logs mount pause prune pull push reload remove rename restart rm run save search start stats stop tag top unmount unpause update wait".split())


def _podman_meta(popenargs, kwargs):
    argv = popenargs[0] if popenargs else kwargs.get("args")
    if not isinstance(argv, (list, tuple)) or not argv:
        return None
    try:
        if os.path.basename(os.fsdecode(argv[0])) != "podman":
            return None
        verbs = [os.fsdecode(x) for x in argv[1:] if not os.fsdecode(x).startswith("-")]
    except (TypeError, ValueError):
        return None
    if not verbs:
        return "other", None
    action = verbs[0] if verbs[0] in _TOP else "other"
    subaction = verbs[1] if action in _GROUPS and len(verbs) > 1 and verbs[1] in _VERBS else None
    return action, subaction


def _timed_run(real_run, spans):
    def run(*popenargs, **kwargs):
        meta = _podman_meta(popenargs, kwargs)
        if meta is None:
            return real_run(*popenargs, **kwargs)
        action, subaction = meta
        started_ns = time.monotonic_ns()
        returncode = None
        try:
            result = real_run(*popenargs, **kwargs)
            returncode = result.returncode
            return result
        except subprocess.CalledProcessError as exc:
            returncode = exc.returncode
            raise
        finally:
            finished_ns = time.monotonic_ns()
            spans.append({"action": action, "subaction": subaction,
                          "started_ns": started_ns, "finished_ns": finished_ns,
                          "duration_ms": round((finished_ns - started_ns) / 1_000_000, 3),
                          "returncode": returncode})
    return run


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse(argv):
    project = output = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--project", "--output"):
            if index + 1 >= len(argv):
                raise ValueError(token + " requires a value")
            value = argv[index + 1]
            index += 2
        elif token.startswith(("--project=", "--output=")):
            token, value = token.split("=", 1)
            index += 1
        else:
            break
        project, output = (value, output) if token.startswith("--project") else (project, value)
    if not project or not output:
        raise ValueError("--project and --output are required")
    return project, output, argv[index:]


def main(argv=None):
    started_ns = time.monotonic_ns()
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--help"], ["-h"]):
        print(USAGE)
        return 0
    try:
        project_arg, output_arg, product_args = _parse(args)
    except ValueError as exc:
        print("trace_startup_entry: " + str(exc), file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    project, output = Path(project_arg), Path(output_arg)
    if not project.is_absolute() or not output.is_absolute():
        print("trace_startup_entry: --project and --output must be absolute", file=sys.stderr)
        return 2
    project, output = project.resolve(), output.resolve()
    if not project.is_dir():
        print("trace_startup_entry: project is not a directory: " + str(project), file=sys.stderr)
        return 2
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(project / "src"), str(project)]
    wrapper = Path(__file__).resolve()
    wrapper_sha256 = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    spans = []
    real_run = subprocess.run
    subprocess.run = _timed_run(real_run, spans)
    import_started_ns = import_finished_ns = None
    product_cli_file = None
    returncode = exception = None
    metadata_failed = False
    try:
        import_started_ns = time.monotonic_ns()
        import multilinux.cli as product_cli
        import_finished_ns = time.monotonic_ns()
        product_cli_file = str(Path(product_cli.__file__).resolve())
        if not Path(product_cli_file).is_relative_to(project):
            raise RuntimeError("multilinux.cli was imported outside --project: " + product_cli_file)
        result = product_cli.main(product_args)
        returncode = 0 if result is None else result
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            returncode = 1
    except SystemExit as exc:
        code = exc.code
        returncode = (code if isinstance(code, int) and not isinstance(code, bool)
                      else (0 if code is None else 1))
        exception = "SystemExit" if returncode else None
        raise
    except BaseException as exc:
        exception = type(exc).__name__
        raise
    finally:
        if import_started_ns is not None and import_finished_ns is None:
            import_finished_ns = time.monotonic_ns()
        subprocess.run = real_run
        finished_ns = time.monotonic_ns()
        import_ms = (None if import_started_ns is None or import_finished_ns is None
                     else round((import_finished_ns - import_started_ns) / 1_000_000, 3))
        metadata = {
            "schema_version": 1, "started_ns": started_ns, "finished_ns": finished_ns,
            "elapsed_ms": round((finished_ns - started_ns) / 1_000_000, 3),
            "returncode": returncode, "exception": exception,
            "project_root": str(project), "product_cli_file": product_cli_file,
            "product_import_started_ns": import_started_ns,
            "product_import_finished_ns": import_finished_ns,
            "product_import_ms": import_ms, "wrapper_file": str(wrapper),
            "wrapper_sha256": wrapper_sha256, "podman_call_count": len(spans),
            "podman_total_ms": round(sum(s["finished_ns"] - s["started_ns"] for s in spans) / 1_000_000, 3),
            "podman_spans": spans,
        }
        try:
            _write_json(output, metadata)
        except Exception as write_exc:
            print("trace_startup_entry: metadata write failed: " + str(write_exc), file=sys.stderr)
            metadata_failed = True
    return 3 if metadata_failed else returncode


if __name__ == "__main__":
    raise SystemExit(main())
