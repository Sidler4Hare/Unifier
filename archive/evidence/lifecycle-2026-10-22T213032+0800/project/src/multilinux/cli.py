"""Command line for environment lifecycle and native package transactions."""

import argparse
import json
import sys

from multilinux.errors import MultilinuxError
from multilinux.packages import transaction_verdict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ml",
        description="Lifecycle, package, desktop and resource commands for "
                    "Debian, Fedora and Arch environments.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    env = commands.add_parser("env", help="create, inspect and run a project-owned environment")
    operations = env.add_subparsers(dest="operation", required=True)

    create = operations.add_parser("create", help="create a stopped environment from the locked image")
    create.add_argument("--target", required=True, help="debian13, fedora44 or arch")
    create.add_argument("--id", help="defaults to the target name")
    create.add_argument("--network", choices=("none", "pasta"), default="none",
                        help="none has no repository access; pasta is required for install and update")
    create.add_argument("--share", action="append", default=None,
                        help="absolute host directory, mounted at the same path; repeatable")
    create.add_argument("--desktop", action="store_true",
                        help="mount the current Wayland, X11 and audio sockets")
    create.add_argument("--userns", choices=("rootless-default", "keep-id"), default="rootless-default",
                        help="keep-id allows applications to run with the host UID/GID")
    create.add_argument('--device', action='append', default=[], help='explicit /dev/videoN camera; requires --desktop')
    create.add_argument('--portals', action='store_true', help='filtered host desktop portal bridge; use an on-demand application')
    create.add_argument('--gpu', action='store_true', help='passthrough host /dev/dri devices for hardware graphics and video; requires --desktop')
    create.add_argument('--ipc', choices=('private', 'host'), default=None, help='IPC namespace mode; desktop defaults to host')
    create.set_defaults(func=cmd_create)

    listing = operations.add_parser("list", help="list environments recorded by this project")
    listing.set_defaults(func=cmd_list)

    inspect = operations.add_parser("inspect", help="show one environment record and live runtime state")
    inspect.add_argument("id")
    inspect.set_defaults(func=cmd_inspect)

    export = operations.add_parser("export", help="write the inspect record to a JSON file")
    export.add_argument("id")
    export.add_argument("--output", required=True)
    export.set_defaults(func=cmd_export)

    start = operations.add_parser("start", help="start an environment's sleeper process")
    start.add_argument("id")
    start.set_defaults(func=cmd_start)

    stop = operations.add_parser("stop", help="stop an environment without deleting it")
    stop.add_argument("id")
    stop.set_defaults(func=cmd_stop)

    remove = operations.add_parser("remove", help="delete an environment and its writable root")
    remove.add_argument("id")
    remove.add_argument("--yes", action="store_true",
                        help="required; this permanently deletes the environment")
    remove.set_defaults(func=cmd_remove)

    # Parsed by cmd_run before this parser runs, so `ml env -h` can still list it.
    run = operations.add_parser("run", help="execute argv in a started environment")
    run.add_argument("id")
    run.add_argument("--workdir")
    run.add_argument("--env", action="append", default=[], metavar="KEY",
                     help="host variable to pass through; repeatable")
    run.set_defaults(func=lambda args: cmd_run_help(args) or 0)

    pkg = commands.add_parser("pkg", help="query, install, update and remove native packages")
    pkg_ops = pkg.add_subparsers(dest="operation", required=True)
    route = pkg_ops.add_parser('route', help='identify the target environment for a local package')
    route.add_argument('file')
    route.set_defaults(func=cmd_pkg_route)

    query = pkg_ops.add_parser("query", help="ask the environment's package database about one name")
    query.add_argument("id")
    query.add_argument("package")
    query.set_defaults(func=cmd_pkg_query)

    install = pkg_ops.add_parser(
        "install", help="install repository packages, or one local file with --file")
    install.add_argument("id")
    install.add_argument("packages", nargs="*", help="repository package names")
    install.add_argument("--file", help="local .deb, .rpm or Arch package to copy in and install")
    install.add_argument("--package", help="package name expected in the inventory after --file")
    install.add_argument("--use-command", action="append", default=None, metavar="ARG",
                         help="argv checked after install; it must exit 0. Repeat for each argument")
    install.set_defaults(func=cmd_pkg_install)

    update = pkg_ops.add_parser("update", help="upgrade packages already installed in the environment")
    update.add_argument("id")
    update.add_argument("--use-command", action="append", default=None, metavar="ARG",
                        help="argv checked after update; it must exit 0. Repeat for each argument")
    update.set_defaults(func=cmd_pkg_update)

    remove = pkg_ops.add_parser("remove", help="remove packages from the environment")
    remove.add_argument("id")
    remove.add_argument("packages", nargs="+")
    remove.add_argument("--use-command", action="append", default=None, metavar="ARG",
                        help="argv checked after remove; it must not exit 0. Repeat for each argument")
    remove.set_defaults(func=cmd_pkg_remove)

    log = pkg_ops.add_parser("log", help="list package transactions for an environment")
    log.add_argument("id")
    log.set_defaults(func=cmd_pkg_log)

    show = pkg_ops.add_parser("show", help="print one package transaction")
    show.add_argument("transaction")
    show.set_defaults(func=cmd_pkg_show)

    app = commands.add_parser("app", help="record and open an application inside an environment")
    app_ops = app.add_subparsers(dest="operation", required=True)
    add = app_ops.add_parser("add", help="record an executable that stays inside its environment")
    add.add_argument("environment")
    add.add_argument("id")
    add.add_argument("--exec", required=True, dest="executable")
    add.add_argument("--workdir", help="absolute path inside a shared mount")
    add.add_argument("--as-host-user", action="store_true",
                     help="run this application with the host UID/GID; requires --userns keep-id")
    add.add_argument('--on-demand', action='store_true', help='start its environment on open and stop after the last managed application exits')
    add.add_argument('--package-file', help='record package format, SHA-256 and target; the installer is not needed on subsequent opens')
    add.add_argument("--set", action="append", default=None, metavar="KEY=VALUE",
                     help="extra environment value stored in the launch record; repeatable")
    add.set_defaults(func=cmd_app_add)
    app_show = app_ops.add_parser("show", help="print one launch record")
    app_show.add_argument("id")
    app_show.set_defaults(func=cmd_app_show)
    opened = app_ops.add_parser("open", help="run the recorded argv; extra arguments follow --")
    opened.add_argument("id")
    opened.set_defaults(func=cmd_app_open)
    execute = app_ops.add_parser("exec", help="run another argv with the application's session environment")
    execute.add_argument("id")
    execute.set_defaults(func=cmd_app_exec)
    exported = app_ops.add_parser("export", help="add a desktop entry and a private file association")
    exported.add_argument("id")
    exported.add_argument("--mime-type", default="application/x-multilinux-note")
    exported.add_argument("--extension", default="mlnote")
    exported.add_argument('--menu-only', action='store_true', help='register an application without changing file associations')
    exported.add_argument('--name', dest='display_name')
    exported.add_argument('--icon')
    exported.add_argument('--wm-class')
    exported.add_argument('--desktop-id', dest='desktop_alias')
    exported.set_defaults(func=cmd_app_export)
    unexport = app_ops.add_parser("unexport", help="remove the desktop entry and file association")
    unexport.add_argument("id")
    unexport.set_defaults(func=cmd_app_unexport)
    app_remove = app_ops.add_parser("remove", help="delete a launch record and its desktop entry")
    app_remove.add_argument("id")
    app_remove.set_defaults(func=cmd_app_remove)

    res = commands.add_parser("res", help="sample cgroup, PSS, disk and host resources")
    res_ops = res.add_subparsers(dest="operation", required=True)
    res_sample = res_ops.add_parser("sample", help="take one read-only resource sample")
    res_sample.add_argument("id")
    res_sample.add_argument("--output", help="write JSON here instead of standard output")
    res_sample.set_defaults(func=cmd_res_sample)
    res_series = res_ops.add_parser("series", help="take a timed series and summarize it")
    res_series.add_argument("id")
    res_series.add_argument("--samples", type=int, default=10)
    res_series.add_argument("--interval", type=float, default=1.0,
                            help="seconds between samples")
    res_series.add_argument("--settle", type=float, default=0.0,
                            help="seconds to wait before the first sample")
    res_series.add_argument("--reset-peak", action="store_true",
                            help="reset memory.peak before sampling")
    res_series.add_argument("--output", help="write JSON here instead of standard output")
    res_series.set_defaults(func=cmd_res_series)
    return parser


def run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ml env run",
        description="Execute argv inside a started environment. "
                    "Write the command after --. The process sees that environment's files, not the host root.",
        epilog="Example: python3 tools/ml.py env run debian13 --workdir /usr -- true",
    )
    parser.add_argument("id")
    parser.add_argument("--workdir", help="absolute path inside the environment")
    parser.add_argument("--env", action="append", default=None, metavar="KEY",
                        help="pass this host variable; the rest of the host environment stays outside")
    return parser


def parse_run_args(argv: list[str]):
    if not argv or argv[0] in {"-h", "--help"}:
        run_parser().print_help()
        return None
    if "--" not in argv:
        raise MultilinuxError("run requires -- before the command argv")
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1:]
    if not command:
        raise MultilinuxError("run requires the command argv after --")
    args = run_parser().parse_args(options)
    args.argv = command
    return args


def default_service():
    from multilinux.service import EnvironmentService
    from multilinux.runtime import PodmanRuntime
    from multilinux.project import load_project

    config, lock, root = load_project()
    return EnvironmentService(root, PodmanRuntime(), config, lock)


def service_factory():
    return default_service()


def cmd_create(args) -> int:
    options = {"user_namespace": args.userns} if args.userns != "rootless-default" else {}
    if args.device:
        options['devices'] = args.device
    if args.portals:
        options['portals'] = True
    if args.gpu:
        options['gpu'] = True
    if args.ipc:
        options['ipc'] = args.ipc
    record = service_factory().create(
        args.target, args.id, args.share or [], args.network, desktop=args.desktop, **options)
    print(f"created {record['id']} ({record['target']}) {record['runtime_state']}")
    return 0


def cmd_list(args) -> int:
    print(json.dumps(service_factory().list_summaries(), ensure_ascii=False, indent=2))
    return 0


def cmd_inspect(args) -> int:
    print(json.dumps(service_factory().inspect(args.id), ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    service_factory().export(args.id, args.output)
    print(f"exported {args.id} {args.output}")
    return 0


def cmd_start(args) -> int:
    view = service_factory().start(args.id)
    print(f"started {args.id} ({view['runtime_state']})")
    return 0


def cmd_stop(args) -> int:
    view = service_factory().stop(args.id)
    print(f"stopped {args.id} ({view['runtime_state']})")
    return 0


def cmd_remove(args) -> int:
    if not args.yes:
        raise MultilinuxError("remove deletes the environment's writable root; pass --yes")
    service_factory().remove(args.id)
    print(f"removed {args.id}")
    return 0


def cmd_run_help(args) -> int:
    run_parser().print_help()
    return 0


def _print_transaction(txn: dict) -> int:
    print(json.dumps(txn, ensure_ascii=False, indent=2))
    verdict = transaction_verdict(txn.get("phase"), txn.get("maintenance"))
    if verdict == "verified-with-warnings":
        items = (txn.get("maintenance") or {}).get("items") or []
        reasons = ", ".join(dict.fromkeys(
            str(item.get("reason")) for item in items if item.get("reason")))
        detail = f" ({reasons})" if reasons else ""
        failures = sum(int(item.get("count") or 1) for item in items)
        print(
            f"ml: warning: {txn.get('id')}: the package changes are applied "
            f"(exit code 0), but {failures} maintenance hook failure(s){detail} "
            "were reported; system integration is unverified and may need a "
            "host step. "
            f"See 'ml pkg show {txn.get('id')}' for the saved output.",
            file=sys.stderr)
        return 0
    return 0 if txn.get("phase") == "completed" else 1


def cmd_pkg_query(args) -> int:
    print(json.dumps(service_factory().query_package(args.id, args.package),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_pkg_install(args) -> int:
    service = service_factory()
    use = args.use_command or []
    if args.file:
        if args.packages:
            raise MultilinuxError("pass either package names or --file")
        txn = service.install_local(args.id, args.file, package_name=args.package, use_argv=use)
    else:
        if not args.packages:
            raise MultilinuxError("install requires package names or --file")
        txn = service.install_packages(args.id, args.packages, use_argv=use)
    return _print_transaction(txn)


def cmd_pkg_update(args) -> int:
    txn = service_factory().update_packages(args.id, use_argv=args.use_command or [])
    return _print_transaction(txn)


def cmd_pkg_remove(args) -> int:
    txn = service_factory().remove_packages(args.id, args.packages, use_argv=args.use_command or [])
    return _print_transaction(txn)


def cmd_pkg_log(args) -> int:
    rows = []
    for txn in service_factory().transactions_for(args.id):
        maintenance = txn.get("maintenance") or {}
        rows.append({
            "id": txn["id"],
            "operation": txn["operation"],
            "packages": txn.get("packages") or [],
            "phase": txn["phase"],
            "verdict": transaction_verdict(txn.get("phase"), txn.get("maintenance")),
            "maintenance": maintenance.get("status"),
            "failure_phase": txn.get("failure_phase"),
            "started_at": txn.get("started_at"),
            "finished_at": txn.get("finished_at"),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _split_double_dash(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in argv:
        return argv, []
    split = argv.index("--")
    return argv[:split], argv[split + 1:]


def _set_overlay(items) -> dict[str, str]:
    overlay = {}
    for item in items or []:
        if "=" not in item:
            raise MultilinuxError("--set must look like KEY=VALUE")
        key, value = item.split("=", 1)
        overlay[key] = value
    return overlay


def cmd_app_add(args) -> int:
    argv = [args.executable, *list(getattr(args, "argv", None) or [])]
    options = {"as_host_user": True} if args.as_host_user else {}
    if args.on_demand:
        options['on_demand'] = True
    if args.package_file:
        from multilinux.routing import route_package
        options['package_origin'] = route_package(args.package_file)
    launch = service_factory().add_app(
        args.environment, args.id, args.executable, argv,
        workdir=args.workdir, env_overlay=_set_overlay(args.set), **options)
    print(f"added {launch['id']} in {launch['environment_id']}")
    return 0


def cmd_app_show(args) -> int:
    print(json.dumps(service_factory().show_app(args.id), ensure_ascii=False, indent=2))
    return 0


def cmd_app_open(args) -> int:
    result = service_factory().open_app(args.id, list(getattr(args, "extra", None) or []))
    return result.returncode


def worker_parser() -> argparse.ArgumentParser:
    """Internal supervisor entry point; never listed by `ml app -h`."""
    parser = argparse.ArgumentParser(prog="ml app _worker")
    parser.add_argument("id")
    return parser


def cmd_app_worker(args) -> int:
    from multilinux.on_demand import managed_open
    result = managed_open(service_factory(), args.id, list(getattr(args, 'extra', None) or []))
    return result.returncode


def cmd_app_exec(args) -> int:
    argv = list(getattr(args, "argv", None) or [])
    if not argv:
        raise MultilinuxError("app exec requires argv after --")
    result = service_factory().exec_app(args.id, argv, capture=False, timeout=None)
    return result.returncode


def cmd_app_export(args) -> int:
    options = {}
    if args.menu_only:
        options = {key: getattr(args, key) for key in ('menu_only', 'display_name', 'icon', 'wm_class', 'desktop_alias')}
    launch = service_factory().export_app(
        args.id, mime_type=args.mime_type, extension=args.extension, **options)
    print(launch["export"]["desktop_path"])
    return 0


def cmd_pkg_route(args):
    from multilinux.routing import route_package
    print(json.dumps(route_package(args.file), ensure_ascii=False, indent=2))
    return 0


def cmd_app_unexport(args) -> int:
    service_factory().unexport_app(args.id)
    print(f"unexported {args.id}")
    return 0


def cmd_app_remove(args) -> int:
    service_factory().remove_app(args.id)
    print(f"removed {args.id}")
    return 0


def _emit_json(data: dict, output: str | None) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if output:
        from pathlib import Path
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


def cmd_res_sample(args) -> int:
    from multilinux.resources import ResourceProbe
    data = ResourceProbe(service_factory()).sample(args.id)
    _emit_json(data, args.output)
    return 0


def cmd_res_series(args) -> int:
    from multilinux.resources import ResourceProbe
    if args.samples > 1:
        print(f"sampling {args.id}: {args.samples} samples every {args.interval}s",
              file=sys.stderr, flush=True)
    data = ResourceProbe(service_factory()).series(
        args.id, samples=args.samples, interval=args.interval,
        settle=args.settle, reset_peak=args.reset_peak)
    _emit_json(data, args.output)
    return 0


def cmd_pkg_show(args) -> int:
    print(json.dumps(service_factory().show_transaction(args.transaction),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_run(argv: list[str]) -> int:
    args = parse_run_args(argv)
    if args is None:
        return 0
    return service_factory().run(args.id, args.argv, workdir=args.workdir,
                                 env_keys=args.env or [])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:2] == ["env", "run"]:
            return cmd_run(argv[2:])
        if argv[:2] == ["app", "_worker"]:
            # The supervised worker is an internal entry point. Parsing it here
            # keeps it out of `ml app -h` while still accepting program argv.
            head, command = _split_double_dash(argv[2:])
            args = worker_parser().parse_args(head)
            args.extra = command
            return cmd_app_worker(args)
        if len(argv) >= 2 and argv[0] == "app" and argv[1] in {"add", "open", "exec"}:
            head, command = _split_double_dash(argv[2:])
            args = build_parser().parse_args([argv[0], argv[1], *head])
            if argv[1] == "open":
                args.extra = command
            else:
                args.argv = command
            return args.func(args)
        args = build_parser().parse_args(argv)
        return args.func(args)
    except (MultilinuxError, OSError, ValueError) as exc:
        print(f"ml: {exc}", file=sys.stderr)
        return 1
