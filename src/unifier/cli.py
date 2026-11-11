"""Command line for environment lifecycle and native package transactions."""

import argparse
import json
import sys

from unifier.errors import UnifierError
from unifier import __version__
from unifier.packages import transaction_verdict
from unifier.native_io import add_package_arguments, package_options


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unifier",
        description="Lifecycle, package, desktop and resource commands for "
                    "Debian, Fedora and Arch environments.",
    )
    parser.add_argument("--version", action="version", version=f"Unifier {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("agent", help="host-issued, scoped guest requests")
    agent_ops = agent.add_subparsers(dest="operation", required=True)
    serve = agent_ops.add_parser("serve", help="serve a private host-issued policy")
    serve.add_argument("--policy", required=True)
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--max-requests", type=int)
    serve.set_defaults(func=cmd_agent_serve)
    request = agent_ops.add_parser("request", help="request one host-authorized action")
    request.add_argument("--socket", required=True)
    request.add_argument("--action", required=True)
    request.add_argument("--token-env", default="UNIFIER_AGENT_TOKEN")
    request.add_argument("--request-uuid")
    request.add_argument("--timeout", type=float, default=60.0)
    request.set_defaults(func=cmd_agent_request)
    compat = commands.add_parser("compat", help="inspect the distro family whitelist and host/guest relationships")
    compat_ops = compat.add_subparsers(dest="operation", required=True)
    families = compat_ops.add_parser("list", help="show explicit distro IDs, host profiles and prepared guests")
    families.set_defaults(func=cmd_compat_list)
    check = compat_ops.add_parser("check", help="check host/guest policy without using the runtime")
    check.add_argument("--target", required=True)
    check.add_argument("--os-release", help="simulate a release file on the current machine/libc; policy only")
    check.set_defaults(func=cmd_compat_check)
    matrix = compat_ops.add_parser("matrix", help="show current host relationships to all prepared guests")
    matrix.add_argument("--os-release", help="simulate a release file on the current machine/libc; policy only")
    matrix.set_defaults(func=cmd_compat_matrix)
    env = commands.add_parser("env", help="create, inspect and run a project-owned environment")
    operations = env.add_subparsers(dest="operation", required=True)

    create = operations.add_parser("create", help="create a stopped environment from the locked image")
    create.add_argument("--target", required=True, help="debian13, fedora44 or arch")
    create.add_argument("--id", help="defaults to the target name")
    create.add_argument("--network", choices=("none", "pasta"), default="none",
                        help="none has no repository access; pasta is required for install and update")
    create.add_argument("--share", action="append", default=None,
                        help="absolute host directory, mounted at the same path; repeatable")
    create.add_argument("--share-label", choices=("preserve-host", "shared-relabel"),
                        default="preserve-host",
                        help="preserve-host (default) adds no :z and never relabels the host; "
                             "shared-relabel explicitly adds :z to ordinary shares")
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

    plan = operations.add_parser(
        "plan",
        help="show whether a change upgrades this environment or replaces it")
    plan.add_argument("id")
    plan.add_argument("--target", help="prepared target id to compare")
    plan.add_argument("--network", choices=("none", "pasta"))
    plan.add_argument("--userns", choices=("rootless-default", "keep-id"))
    plan.add_argument("--desktop", action=argparse.BooleanOptionalAction, default=None,
                      help="compare a desktop session; --no-desktop compares a headless one")
    plan.add_argument("--share", action="append", default=None,
                      help="absolute host directory; the whole set is compared")
    plan.add_argument("--share-label", choices=("preserve-host", "shared-relabel"),
                      help="compare a share-label policy change; it requires a new environment")
    plan.add_argument("--ipc", choices=("private", "host"))
    plan.set_defaults(func=cmd_plan)

    migrate = operations.add_parser(
        "migrate",
        help="explicitly add versioned identity to one valid legacy record (no runtime calls)")
    migrate.add_argument("id")
    migrate.set_defaults(func=cmd_env_migrate)

    # Parsed by cmd_run before this parser runs, so `unifier env -h` can still list it.
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
    add_package_arguments(install, repair=True)

    repair = pkg_ops.add_parser("repair-deps", help="find and install missing runtime library providers")
    repair.add_argument("id")
    repair.add_argument("packages", nargs="*", help="installed packages whose ELF files are checked")
    repair.add_argument("--library", action="append", default=[], help="exact missing SONAME from a runtime error")
    repair.add_argument("--from-log", metavar="FILE",
                        help="extract exact missing SONAMEs from a saved loader/dlopen error log")
    repair.add_argument("--diagnose-only", action="store_true",
                        help="classify --from-log locally without accessing an environment or installing packages")
    add_package_arguments(repair)
    repair.set_defaults(func=cmd_pkg_repair)

    recover = pkg_ops.add_parser("recover", help="audit and reconcile an interrupted package transaction")
    recover.add_argument("id")
    recover.set_defaults(func=cmd_pkg_recover)

    update = pkg_ops.add_parser("update", help="upgrade packages already installed in the environment")
    update.add_argument("id")
    update.add_argument("--use-command", action="append", default=None, metavar="ARG",
                        help="argv checked after update; it must exit 0. Repeat for each argument")
    update.set_defaults(func=cmd_pkg_update)
    add_package_arguments(update)

    remove = pkg_ops.add_parser("remove", help="remove packages from the environment")
    remove.add_argument("id")
    remove.add_argument("packages", nargs="+")
    remove.add_argument("--use-command", action="append", default=None, metavar="ARG",
                        help="argv checked after remove; it must not exit 0. Repeat for each argument")
    remove.set_defaults(func=cmd_pkg_remove)
    add_package_arguments(remove)

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
    add.add_argument('--headless', action='store_true',
                     help='register a CLI application in an environment without desktop bindings')
    add.add_argument('--package-file', help='record package format, SHA-256 and target; the installer is not needed on subsequent opens')
    add.add_argument("--set", action="append", default=None, metavar="KEY=VALUE",
                     help="extra environment value stored in the launch record; repeatable")
    add.set_defaults(func=cmd_app_add)
    app_show = app_ops.add_parser("show", help="print one launch record")
    app_show.add_argument("id")
    app_show.set_defaults(func=cmd_app_show)
    from unifier.file_chooser import MODES

    chooser = app_ops.add_parser("file-chooser", help="show or select the application's file chooser mode")
    chooser.add_argument("id")
    chooser.add_argument("mode", nargs="?", choices=MODES)
    chooser.set_defaults(func=cmd_app_file_chooser)
    leases = app_ops.add_parser('leases', help='inspect shared application and project leases')
    leases.add_argument('environment')
    leases.set_defaults(func=cmd_app_leases)
    recover = app_ops.add_parser('recover', help='reconcile application leases using guest execution proof')
    recover.add_argument('environment')
    recover.add_argument('--token')
    recover.set_defaults(func=cmd_app_recover)
    cancel = app_ops.add_parser('cancel', help='cancel one identity-bound application execution')
    cancel.add_argument('environment')
    cancel.add_argument('token')
    cancel.set_defaults(func=cmd_app_cancel)
    opened = app_ops.add_parser("open", help="run the recorded argv; extra arguments follow --")
    opened.add_argument("id")
    opened.add_argument("--project", help="require this accepted project UUID pin")
    opened.add_argument("--integration",
                        help="require this installed desktop integration journal")
    opened.add_argument("--app-instance",
                        help="require the exact exported application instance")
    opened.add_argument("--execution-fingerprint",
                        help="require the exported application execution fingerprint")
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

    root = commands.add_parser("root", help="register independent host data roots")
    root_ops = root.add_subparsers(dest="operation", required=True)
    root_add = root_ops.add_parser("add", help="register a local host directory; never mounts it")
    root_add.add_argument("path")
    root_add.add_argument("--backend", default="local-directory")
    root_add.add_argument("--domain", dest="management_domain", default="host-project")
    root_add.add_argument("--policy", dest="access_policy",
                          choices=("read-only", "read-write"),
                          help="defaults to read-write; a --source-view root is read-only")
    root_add.add_argument(
        "--cleanup-policy", choices=("retain", "controlled"), default="retain",
        help="retain never deletes a move source; controlled enables the bounded "
             "lease-and-quarantine move only with an explicit --cleanup-source")
    root_add.add_argument(
        "--source-view", dest="source_view_uuid",
        help="register a ready read-only C view mount as a read-only source root")
    root_add.set_defaults(func=cmd_root_add)
    root_list = root_ops.add_parser("list", help="list data-root registrations")
    root_list.set_defaults(func=cmd_root_list)
    root_show = root_ops.add_parser("show", help="show one registration by UUID or locator")
    root_show.add_argument("identifier")
    root_show.set_defaults(func=cmd_root_show)
    root_remove = root_ops.add_parser(
        "remove", help="remove only the registration; the directory is never deleted")
    root_remove.add_argument("identifier")
    root_remove.set_defaults(func=cmd_root_remove)
    root_match = root_ops.add_parser(
        "match", help="select the longest registered root for an absolute path")
    root_match.add_argument("path")
    root_match.set_defaults(func=cmd_root_match)

    file_command = commands.add_parser(
        "file", help="resolve a read-only FileRef inside a registered data root")
    file_ops = file_command.add_subparsers(dest="operation", required=True)
    file_resolve = file_ops.add_parser(
        "resolve", help="observe one regular file; symlinks are refused")
    file_resolve.add_argument("root", nargs="?", help="root UUID")
    file_resolve.add_argument("relative", nargs="?", help="path relative to that root")
    file_resolve.add_argument("--path", dest="absolute_path",
                              help="select the longest registered root for this absolute path")
    file_resolve.add_argument("--role", default="document",
                              help="bounded file-role label; never an interpreter selector")
    file_resolve.set_defaults(func=cmd_file_resolve)

    for name, function, help_text in (
            ("copy", cmd_file_copy,
             "copy one regular file between registered roots; never overwrites"),
            ("move", cmd_file_move,
             "copy and, only with --cleanup-source plus a controlled source root, "
             "remove the original source; otherwise retain it and exit 1")):
        operation = file_ops.add_parser(name, help=help_text)
        operation.add_argument("source_root", help="registered source root UUID")
        operation.add_argument("source_relative", help="path relative to the source root")
        operation.add_argument("target_root", help="registered read-write target root UUID")
        operation.add_argument("target_relative", help="path relative to the target root")
        if name == "move":
            operation.add_argument(
                "--cleanup-source", action="store_true",
                help="explicitly authorize removal of this source after the target "
                     "is published, revalidated and registered; requires a "
                     "read-write local source root registered --cleanup-policy controlled")
        operation.set_defaults(func=function)
    file_status = file_ops.add_parser(
        "status", help="show one file operation or all recorded operations")
    file_status.add_argument("operation", nargs="?")
    file_status.set_defaults(func=cmd_file_status)
    file_recover = file_ops.add_parser(
        "recover", help="idempotently recover one operation or all pending operations")
    file_recover.add_argument("operation", nargs="?")
    file_recover.set_defaults(func=cmd_file_recover)
    file_list = file_ops.add_parser(
        "list", help="list file operation journals and their conservative states")
    file_list.set_defaults(func=cmd_file_list)

    project = commands.add_parser("project", help="register a host project and its toolchain")
    project_ops = project.add_subparsers(dest="operation", required=True)
    project_add = project_ops.add_parser("add", help="explicitly accept a root and environment")
    project_add.add_argument("--root", required=True, help="registered data root UUID")
    project_add.add_argument("--environment", required=True, help="environment id")
    project_add.add_argument("--default-save-root",
                             help="absolute directory inside the root; defaults to the root")
    project_add.add_argument("--policy", choices=("read-only", "read-write"),
                             default="read-write")
    project_add.add_argument("--alias")
    project_add.add_argument("--handler", action="append", default=[], metavar="INTENT=UUID",
                             help="bind an existing handler; repeatable")
    project_add.set_defaults(func=cmd_project_add)
    project_list = project_ops.add_parser("list", help="list project registrations")
    project_list.set_defaults(func=cmd_project_list)
    project_show = project_ops.add_parser("show", help="show one project")
    project_show.add_argument("identifier")
    project_show.set_defaults(func=cmd_project_show)
    project_remove = project_ops.add_parser(
        "remove", help="remove only the project registration; data and handlers stay")
    project_remove.add_argument("identifier")
    project_remove.set_defaults(func=cmd_project_remove)
    project_plan = project_ops.add_parser(
        "plan", help="show the accepted project session pins without starting anything")
    project_plan.add_argument("project")
    project_plan.add_argument("--cwd", help="existing absolute project directory")
    project_plan.add_argument("--env", action="append", default=[], metavar="KEY",
                              help="host variable to allowlist explicitly; repeatable")
    project_plan.add_argument("--mode", choices=("run", "shell"), default="run")
    project_plan.set_defaults(func=cmd_project_plan)
    project_run = project_ops.add_parser(
        "run", help="run one argv array in a project session; command follows --")
    project_run.add_argument("project")
    project_run.add_argument("--cwd")
    project_run.add_argument("--env", action="append", default=[], metavar="KEY")
    project_run.add_argument("--tty", action="store_true",
                             help="allocate a TTY only on an actual terminal")
    project_run.add_argument("--integration",
                             help="require this installed desktop integration journal")
    project_run.set_defaults(func=cmd_project_run)
    project_shell = project_ops.add_parser(
        "shell", help="enter one project shell; later commands are ordinary guest commands")
    project_shell.add_argument("project")
    project_shell.add_argument("--cwd")
    project_shell.add_argument("--env", action="append", default=[], metavar="KEY")
    project_shell.add_argument("--tty", action=argparse.BooleanOptionalAction, default=None,
                               help="request a TTY; defaults to an actual interactive terminal")
    project_shell.add_argument("--integration",
                               help="require this installed desktop integration journal")
    project_shell.set_defaults(func=cmd_project_shell)
    project_session = project_ops.add_parser(
        "session", help="inspect, recover or retire project session records")
    session_ops = project_session.add_subparsers(dest="session_operation", required=True)
    session_list = session_ops.add_parser("list", help="list project sessions and liveness")
    session_list.set_defaults(func=cmd_project_session_list)
    session_show = session_ops.add_parser("show", help="show one session")
    session_show.add_argument("session")
    session_show.set_defaults(func=cmd_project_session_show)
    session_recover = session_ops.add_parser(
        "recover", help="release only dead lease records; keep live/unresolved executions")
    session_recover.add_argument("session", nargs="?")
    session_recover.set_defaults(func=cmd_project_session_recover)
    session_cancel = session_ops.add_parser(
        "cancel", help="signal one session worker/exact guest; never the shared container")
    session_cancel.add_argument("session")
    session_cancel.add_argument("--signal", dest="cancel_signal",
                                choices=("INT", "TERM", "HUP", "KILL"), default="TERM")
    session_cancel.set_defaults(func=cmd_project_session_cancel)
    session_retire = session_ops.add_parser(
        "retire", help="retire a dead record; never deletes the environment")
    session_retire.add_argument("session")
    session_retire.set_defaults(func=cmd_project_session_retire)
    project_export = project_ops.add_parser(
        "export", help="journal and install a reversible project/app desktop entry")
    project_export.add_argument("project")
    project_export.add_argument("--session", dest="session_uuid")
    project_export.add_argument("--app", dest="app_id")
    project_export.add_argument("--name", dest="display_name")
    project_export.add_argument("--icon")
    project_export.add_argument("--wm-class")
    project_export.add_argument("--mime-type")
    project_export.add_argument("--extension")
    project_export.add_argument("--desktop-id", dest="desktop_alias")
    project_export.add_argument("--menu-only", action="store_true")
    project_export.add_argument("--terminal", action=argparse.BooleanOptionalAction,
                                default=False)
    project_export.set_defaults(func=cmd_project_export)
    project_revoke = project_ops.add_parser(
        "revoke", help="restore exact prior desktop bytes when our state is unchanged")
    project_revoke.add_argument("identifier")
    project_revoke.set_defaults(func=cmd_project_revoke)
    project_integration = project_ops.add_parser(
        "integration", help="inspect or idempotently recover desktop integration journals")
    integration_ops = project_integration.add_subparsers(
        dest="integration_operation", required=True)
    integration_list = integration_ops.add_parser("list", help="list journals")
    integration_list.set_defaults(func=cmd_project_integration_list)
    integration_show = integration_ops.add_parser("show", help="show one journal")
    integration_show.add_argument("identifier")
    integration_show.set_defaults(func=cmd_project_integration_show)
    integration_recover = integration_ops.add_parser(
        "recover", help="reconcile journals without overwriting external edits")
    integration_recover.set_defaults(func=cmd_project_integration_recover)

    handler = commands.add_parser("handler", help="bind a stable handler to an environment app")
    handler_ops = handler.add_subparsers(dest="operation", required=True)
    handler_add = handler_ops.add_parser("add", help="register one app handler explicitly")
    handler_add.add_argument("app")
    handler_add.add_argument("--intent", choices=("edit", "view"), required=True)
    handler_add.add_argument("--provider", default="environment-app")
    handler_add.add_argument("--save-capability",
                             choices=("explicit-path", "dialog-adapter", "unknown"),
                             default="unknown")
    handler_add.add_argument("--alias")
    handler_add.add_argument("--argv-template", action="append", default=None,
                             metavar="ARG",
                             help="complete argv template containing exactly one {file}; "
                                  "repeat for each argument; default appends the file path")
    handler_add.set_defaults(func=cmd_handler_add)
    handler_list = handler_ops.add_parser("list", help="list handler registrations")
    handler_list.set_defaults(func=cmd_handler_list)
    handler_show = handler_ops.add_parser("show", help="show one handler")
    handler_show.add_argument("identifier")
    handler_show.set_defaults(func=cmd_handler_show)
    handler_remove = handler_ops.add_parser(
        "remove", help="remove only the handler registration; app/environment/data stay")
    handler_remove.add_argument("identifier")
    handler_remove.set_defaults(func=cmd_handler_remove)

    file_plan = file_ops.add_parser(
        "plan", help="show the accepted project/handler route for one file")
    file_plan.add_argument("--path", required=True, dest="absolute_path")
    file_plan.add_argument("--intent", choices=("edit", "view"), default="edit")
    file_plan.set_defaults(func=cmd_file_plan)
    file_open = file_ops.add_parser(
        "open", help="revalidate and run the accepted route for one file")
    file_open.add_argument("--path", required=True, dest="absolute_path")
    file_open.add_argument("--intent", choices=("edit", "view"), default="edit")
    file_open.set_defaults(func=cmd_file_open)

    view = commands.add_parser(
        "view", help="manage read-only views of stopped environments")
    view_ops = view.add_subparsers(dest="operation", required=True)
    view_open = view_ops.add_parser("open", help="mount a read-only view without starting the environment")
    view_open.add_argument("environment")
    view_open.add_argument("--mountpoint",
                           help="new host mountpoint; defaults to a private directory under .lab/state/views")
    view_open.set_defaults(func=cmd_view_open)
    view_list = view_ops.add_parser("list", help="list view leases and their liveness")
    view_list.set_defaults(func=cmd_view_list)
    view_status = view_ops.add_parser("status", help="show one view or all views")
    view_status.add_argument("identifier", nargs="?")
    view_status.set_defaults(func=cmd_view_status)
    view_revoke = view_ops.add_parser(
        "revoke", help="stop the worker/broker, unmount and release the view lease")
    view_revoke.add_argument("identifier")
    view_revoke.set_defaults(func=cmd_view_revoke)
    view_recover = view_ops.add_parser(
        "recover", help="clean stale view records without lazy unmount")
    view_recover.add_argument("identifier", nargs="?")
    view_recover.set_defaults(func=cmd_view_recover)
    view_readlink = view_ops.add_parser(
        "readlink-raw", help="return the raw stored symlink text through the view control socket")
    view_readlink.add_argument("identifier")
    view_readlink.add_argument("path")
    view_readlink.set_defaults(func=cmd_view_readlink_raw)

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
        prog="unifier env run",
        description="Execute argv inside a started environment. "
                    "Write the command after --. The process sees that environment's files, not the host root.",
        epilog="Example: python3 tools/unifier.py env run debian13 --workdir /usr -- true",
    )
    parser.add_argument("id")
    parser.add_argument("--workdir", help="absolute path inside the environment")
    parser.add_argument("--env", action="append", default=None, metavar="KEY",
                        help="pass this host variable; the rest of the host environment stays outside")
    return parser


def file_worker_parser() -> argparse.ArgumentParser:
    """Internal detached file supervisor; never listed by `file -h`."""
    parser = argparse.ArgumentParser(prog="unifier file _worker")
    parser.add_argument("--path", required=True, dest="absolute_path")
    parser.add_argument("--intent", choices=("edit", "view"), default="edit")
    return parser


def parse_run_args(argv: list[str]):
    if not argv or argv[0] in {"-h", "--help"}:
        run_parser().print_help()
        return None
    if "--" not in argv:
        raise UnifierError("run requires -- before the command argv")
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1:]
    if not command:
        raise UnifierError("run requires the command argv after --")
    args = run_parser().parse_args(options)
    args.argv = command
    return args


def project_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="unifier project run")
    parser.add_argument("project")
    parser.add_argument("--cwd")
    parser.add_argument("--env", action="append", default=[], metavar="KEY")
    parser.add_argument("--tty", action="store_true")
    return parser


def parse_project_run_args(argv: list[str]):
    if not argv or argv[0] in {"-h", "--help"}:
        project_run_parser().print_help()
        return None
    if "--" not in argv:
        raise UnifierError("project run requires -- before the command argv")
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1:]
    if not command:
        raise UnifierError("project run requires the command argv after --")
    args = project_run_parser().parse_args(options)
    args.argv = command
    return args


def project_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="unifier project _worker")
    parser.add_argument("--session", required=True)
    return parser


def shortcut_context_factory(**kwargs):
    from unifier import shortcuts
    return shortcuts.build_context(**kwargs)


def default_service():
    from unifier.service import EnvironmentService
    from unifier.runtime import PodmanRuntime
    from unifier.install import load_catalog, state_root

    config, lock = load_catalog()
    return EnvironmentService(state_root(), PodmanRuntime(), config, lock)


def service_factory():
    return default_service()


def _state_root():
    from unifier.install import state_root
    return state_root()


def control_factory():
    from unifier.control import Control

    return Control(_state_root())


def routing_factory():
    from unifier.file_routes import FileRoutes

    return FileRoutes(_state_root(), service_factory())


def session_factory():
    from unifier.project_sessions import ProjectSessions

    return ProjectSessions(_state_root(), service_factory())


def integration_factory():
    from unifier.desktop_integrations import DesktopIntegrationManager

    return DesktopIntegrationManager(_state_root(), service_factory())


def view_factory():
    from unifier.views import ViewManager

    return ViewManager(_state_root(), service_factory())


def cmd_agent_serve(args) -> int:
    from unifier.agent_bridge import main as bridge_main

    argv = ["--policy", args.policy, "--root", str(_state_root())]
    if args.once:
        argv.append("--once")
    if args.max_requests is not None:
        argv.extend(["--max-requests", str(args.max_requests)])
    return bridge_main(argv)


def cmd_agent_request(args) -> int:
    from unifier.bridge_client import main as client_main

    argv = ["--socket", args.socket, "--action", args.action,
            "--token-env", args.token_env, "--timeout", str(args.timeout)]
    if args.request_uuid is not None:
        argv.extend(["--request-uuid", args.request_uuid])
    return client_main(argv)


def cmd_compat_list(args) -> int:
    from unifier.compatibility import family_catalog
    print(json.dumps(family_catalog(), ensure_ascii=False, indent=2))
    return 0


def _compat_plans(args) -> list[dict]:
    from pathlib import Path
    from unifier.compatibility import compatibility_mode, observe_host, relationship_plan
    from tools import lab
    config = lab.targets()
    compatibility_mode(config)
    facts = observe_host(Path(args.os_release) if args.os_release else None)
    targets = [args.target] if hasattr(args, "target") else list(config["targets"])
    return [relationship_plan(facts, name, config["targets"].get(name)) for name in targets]


def cmd_compat_check(args) -> int:
    plan = _compat_plans(args)[0]
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan["allowed"] else 1


def cmd_compat_matrix(args) -> int:
    plans = _compat_plans(args)
    print(json.dumps(plans, ensure_ascii=False, indent=2))
    return 0 if any(plan["allowed"] for plan in plans) else 1


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
    # The service default is already preserve-host. Only pass the keyword for
    # an explicit relabel request, keeping old callers/test doubles compatible.
    if args.share_label != "preserve-host":
        options = {**options, "share_label_policy": args.share_label}
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


def cmd_plan(args) -> int:
    desired = {}
    if args.target:
        desired["target"] = args.target
    if args.network:
        desired["network"] = args.network
    if args.userns:
        desired["user_namespace"] = args.userns
    if args.desktop is not None:
        desired["desktop"] = args.desktop
    if args.share is not None:
        desired["shares"] = list(args.share)
    if args.ipc:
        desired["ipc"] = args.ipc
    if args.share_label:
        desired["share_label_policy"] = args.share_label
    print(json.dumps(service_factory().change_plan(args.id, desired or None),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_env_migrate(args) -> int:
    print(json.dumps(service_factory().migrate_environment(args.id),
                     ensure_ascii=False, indent=2))
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
        raise UnifierError("remove deletes the environment's writable root; pass --yes")
    service_factory().remove(args.id)
    print(f"removed {args.id}")
    return 0


def cmd_run_help(args) -> int:
    run_parser().print_help()
    return 0


def _print_transaction(txn: dict) -> int:
    print(json.dumps(txn, ensure_ascii=False, indent=2))
    if (txn.get('dependency_repair') or {}).get('status') == 'bundled-candidates-unverified':
        print('unifier: bundled library candidates found; runtime loading and application '
              'workflow remain unverified.', file=sys.stderr)
    verdict = transaction_verdict(txn.get("phase"), txn.get("maintenance"))
    if verdict == "verified-with-warnings":
        items = (txn.get("maintenance") or {}).get("items") or []
        reasons = ", ".join(dict.fromkeys(
            str(item.get("reason")) for item in items if item.get("reason")))
        detail = f" ({reasons})" if reasons else ""
        failures = sum(int(item.get("count") or 1) for item in items)
        print(
            f"unifier: warning: {txn.get('id')}: the package changes are applied "
            f"(exit code 0), but {failures} maintenance hook failure(s){detail} "
            "were reported; system integration is unverified and may need a "
            "host step. "
            f"See 'unifier pkg show {txn.get('id')}' for the saved output.",
            file=sys.stderr)
        return 0
    return 0 if txn.get("phase") == "completed" else int(txn.get("exit_code") or 1)


def cmd_pkg_query(args) -> int:
    print(json.dumps(service_factory().query_package(args.id, args.package),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_pkg_install(args) -> int:
    service = service_factory()
    use = args.use_command or []
    options = package_options(args, repair=True)
    if args.file:
        if args.packages:
            raise UnifierError("pass either package names or --file")
        txn = service.install_local(args.id, args.file, package_name=args.package, use_argv=use, **options)
    else:
        if not args.packages:
            raise UnifierError("install requires package names or --file")
        txn = service.install_packages(args.id, args.packages, use_argv=use, **options)
    return _print_transaction(txn)


def cmd_pkg_repair(args) -> int:
    diagnostic_path = getattr(args, "from_log", None)
    if getattr(args, "diagnose_only", False):
        if not diagnostic_path or args.packages or args.library:
            raise UnifierError("--diagnose-only requires --from-log without packages or --library")
        from unifier.dependency_diagnostics import read_diagnostics
        report = read_diagnostics(diagnostic_path)
        candidate = bool(report["libraries"]) and all(
            issue["kind"] == "missing-library" for issue in report["issues"])
        print(json.dumps({"mode": "diagnostic-only", "library_repair_candidate": candidate,
                          "provider_resolution": "not-run",
                          "runtime_loading": "unverified", "workflow": "unverified",
                          **report}, ensure_ascii=False, indent=2))
        return 0
    options = package_options(args)
    if diagnostic_path:
        options["diagnostics_path"] = diagnostic_path
    txn = service_factory().repair_dependencies(args.id, args.packages, libraries=args.library,
                                                **options)
    return _print_transaction(txn)


def cmd_pkg_recover(args) -> int:
    print(json.dumps(service_factory().recover_packages(args.id), ensure_ascii=False, indent=2))
    return 0


def cmd_pkg_update(args) -> int:
    txn = service_factory().update_packages(args.id, use_argv=args.use_command or [],
                                           **package_options(args))
    return _print_transaction(txn)


def cmd_pkg_remove(args) -> int:
    txn = service_factory().remove_packages(args.id, args.packages, use_argv=args.use_command or [],
                                           **package_options(args))
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
            raise UnifierError("--set must look like KEY=VALUE")
        key, value = item.split("=", 1)
        overlay[key] = value
    return overlay


def cmd_app_add(args) -> int:
    from unifier.file_chooser import MODE_KEY

    argv = [args.executable, *list(getattr(args, "argv", None) or [])]
    options = {"as_host_user": True} if args.as_host_user else {}
    overlay = _set_overlay(args.set)
    if args.on_demand:
        options['on_demand'] = True
    if getattr(args, "headless", False):
        if overlay.get(MODE_KEY, "headless") != "headless":
            raise UnifierError("--headless conflicts with a graphical file chooser mode")
        overlay[MODE_KEY] = "headless"
        options["desktop_required"] = False
    if args.package_file:
        options["package_origin"] = _route_package(args.package_file)
    launch = service_factory().add_app(
        args.environment, args.id, args.executable, argv,
        workdir=args.workdir, env_overlay=overlay, **options)
    print(f"added {launch['id']} in {launch['environment_id']}")
    return 0


def cmd_app_show(args) -> int:
    print(json.dumps(service_factory().show_app(args.id), ensure_ascii=False, indent=2))
    return 0


def cmd_app_file_chooser(args) -> int:
    from unifier.file_chooser import MODE_KEY

    service = service_factory()
    if args.mode is not None:
        launch = service.set_app_file_chooser(args.id, args.mode)
    else:
        launch = service.show_app(args.id)
    print(launch["env"].get(MODE_KEY, "native"))
    return 0


def _app_group(args):
    from unifier.on_demand import LaunchGroup
    from unifier.service import validate_env_id
    service = service_factory()
    env_id = validate_env_id(args.environment)
    service.store.load(env_id)
    return LaunchGroup(service, env_id)


def cmd_app_leases(args):
    print(json.dumps(_app_group(args).read(), ensure_ascii=False, indent=2))
    return 0


def cmd_app_recover(args):
    result = _app_group(args).recover_applications(getattr(args, 'token', None))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if any(item['action'] in {'kept-unknown', 'recovery-required'} for item in result) else 0


def cmd_app_cancel(args):
    from unifier.application_leases import cancel_application
    print(json.dumps(cancel_application(_app_group(args), args.token), ensure_ascii=False, indent=2))
    return 0


def cmd_app_open(args) -> int:
    project = getattr(args, "project", None)
    integration = getattr(args, "integration", None)
    app_instance = getattr(args, "app_instance", None)
    fingerprint = getattr(args, "execution_fingerprint", None)
    if (app_instance is not None or fingerprint is not None) and integration is None:
        raise UnifierError(
            "application instance/fingerprint pins require a desktop integration")
    project_uuid = None
    if project:
        project_uuid = session_factory().projects.show(project)["project_uuid"]
    if integration:
        integration_factory().validate_launcher(
            integration, project_uuid=project_uuid, app_id=args.id,
            app_instance_uuid=app_instance,
            execution_fingerprint=fingerprint)
    if project:
        session_factory().validate_app_project(project, args.id)
    pins = {}
    if app_instance is not None:
        pins["expected_app_instance_uuid"] = app_instance
    if fingerprint is not None:
        pins["expected_execution_fingerprint"] = fingerprint
    if integration is not None:
        pins["integration_uuid"] = integration
    if project_uuid is not None:
        pins["project_uuid"] = project_uuid
    result = service_factory().open_app(
        args.id, list(getattr(args, "extra", None) or []), **pins)
    return result.returncode


def worker_parser() -> argparse.ArgumentParser:
    """Internal supervisor entry point; never listed by `unifier app -h`."""
    parser = argparse.ArgumentParser(prog="unifier app _worker")
    parser.add_argument("id")
    parser.add_argument("--integration")
    parser.add_argument("--app-instance", dest="app_instance")
    parser.add_argument("--execution-fingerprint", dest="execution_fingerprint")
    parser.add_argument("--project")
    return parser


def cmd_app_worker(args) -> int:
    from unifier.on_demand import managed_open
    pins = {}
    if getattr(args, 'integration', None) is not None:
        pins['integration_uuid'] = args.integration
    if getattr(args, 'app_instance', None) is not None:
        pins['expected_app_instance_uuid'] = args.app_instance
    if getattr(args, 'execution_fingerprint', None) is not None:
        pins['expected_execution_fingerprint'] = args.execution_fingerprint
    if getattr(args, 'project', None) is not None:
        pins['project_uuid'] = args.project
    result = managed_open(
        service_factory(), args.id, list(getattr(args, 'extra', None) or []), **pins)
    return result.returncode


def cmd_app_exec(args) -> int:
    argv = list(getattr(args, "argv", None) or [])
    if not argv:
        raise UnifierError("app exec requires argv after --")
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


def _route_package(path: str) -> dict:
    from unifier.routing import route_package
    service = service_factory()
    return route_package(path, service.config.get("targets"))


def cmd_pkg_route(args):
    print(json.dumps(_route_package(args.file), ensure_ascii=False, indent=2))
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


def _intent_handlers(items) -> dict:
    handlers = {}
    for item in items or []:
        if not isinstance(item, str) or "=" not in item:
            raise UnifierError("--handler must look like INTENT=UUID")
        intent, identifier = item.split("=", 1)
        if not intent or not identifier:
            raise UnifierError("--handler must look like INTENT=UUID")
        handlers[intent] = identifier
    return handlers


def cmd_project_add(args) -> int:
    record = routing_factory().add_project(
        args.root, args.environment, alias=args.alias,
        default_save_root=args.default_save_root, access_policy=args.policy,
        intent_handlers=_intent_handlers(args.handler))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_project_list(args) -> int:
    print(json.dumps(routing_factory().list_projects(), ensure_ascii=False, indent=2))
    return 0


def cmd_project_show(args) -> int:
    print(json.dumps(routing_factory().show_project(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_project_remove(args) -> int:
    print(json.dumps(routing_factory().remove_project(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_project_plan(args) -> int:
    prepared = session_factory().prepare(
        args.project, cwd=args.cwd, argv=[], env_keys=args.env, mode=args.mode)
    print(json.dumps(prepared, ensure_ascii=False, indent=2))
    return 0


def _project_integration_pin(project_identifier: str, integration_uuid):
    if integration_uuid is None:
        return None
    sessions = session_factory()
    project = sessions.projects.show(project_identifier)
    integration_factory().validate_launcher(
        integration_uuid, project_uuid=project["project_uuid"], app_id=None)
    return integration_uuid


def cmd_project_run(args) -> int:
    integration = _project_integration_pin(
        args.project, getattr(args, "integration", None))
    result = session_factory().run(
        args.project, list(args.argv), cwd=args.cwd, env_keys=args.env,
        tty=bool(args.tty), integration_uuid=integration)
    return result["returncode"]


def cmd_project_shell(args) -> int:
    integration = _project_integration_pin(
        args.project, getattr(args, "integration", None))
    result = session_factory().shell(
        args.project, cwd=args.cwd, env_keys=args.env, tty=args.tty,
        integration_uuid=integration)
    return result["returncode"]


def cmd_project_session_list(args) -> int:
    print(json.dumps(session_factory().list(), ensure_ascii=False, indent=2))
    return 0


def cmd_project_session_show(args) -> int:
    print(json.dumps(session_factory().show(args.session),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_project_session_recover(args) -> int:
    print(json.dumps(session_factory().recover(args.session),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_project_session_cancel(args) -> int:
    import signal
    signum = getattr(signal, "SIG" + args.cancel_signal)
    print(json.dumps(session_factory().cancel(args.session, signum),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_project_session_retire(args) -> int:
    print(json.dumps(session_factory().retire(args.session),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_project_export(args) -> int:
    record = integration_factory().install(
        args.project, session_uuid=args.session_uuid, app_id=args.app_id,
        name=args.display_name, icon=args.icon, wm_class=args.wm_class,
        mime_type=args.mime_type, extension=args.extension,
        desktop_alias=args.desktop_alias, menu_only=args.menu_only,
        terminal=args.terminal)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_project_revoke(args) -> int:
    result = integration_factory().revoke(args.identifier)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_project_integration_list(args) -> int:
    print(json.dumps(integration_factory().list(), ensure_ascii=False, indent=2))
    return 0


def cmd_project_integration_show(args) -> int:
    print(json.dumps(integration_factory().show(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_project_integration_recover(args) -> int:
    print(json.dumps(integration_factory().recover(), ensure_ascii=False, indent=2))
    return 0



def cmd_handler_add(args) -> int:
    record = routing_factory().add_handler(
        args.app, intent=args.intent, provider=args.provider,
        save_capability=args.save_capability, alias=args.alias,
        argv_template=args.argv_template)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_handler_list(args) -> int:
    print(json.dumps(routing_factory().list_handlers(), ensure_ascii=False, indent=2))
    return 0


def cmd_handler_show(args) -> int:
    print(json.dumps(routing_factory().show_handler(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_handler_remove(args) -> int:
    print(json.dumps(routing_factory().remove_handler(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_file_plan(args) -> int:
    print(json.dumps(routing_factory().plan(args.absolute_path, args.intent),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_file_open(args) -> int:
    result = routing_factory().open(args.absolute_path, args.intent)
    return result.returncode


def cmd_file_worker(args) -> int:
    result = routing_factory().worker_open(args.absolute_path, args.intent)
    return result.returncode


def cmd_view_open(args) -> int:
    record = view_factory().open(args.environment, mountpoint=args.mountpoint)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_view_list(args) -> int:
    print(json.dumps(view_factory().list(), ensure_ascii=False, indent=2))
    return 0


def cmd_view_status(args) -> int:
    print(json.dumps(view_factory().status(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_view_revoke(args) -> int:
    print(json.dumps(view_factory().revoke(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_view_recover(args) -> int:
    print(json.dumps(view_factory().recover(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_view_readlink_raw(args) -> int:
    print(json.dumps(view_factory().readlink_raw(args.identifier, args.path),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_root_add(args) -> int:
    options = {
        "backend": args.backend,
        "management_domain": args.management_domain,
        "cleanup_policy": args.cleanup_policy,
        "source_view_uuid": args.source_view_uuid,
    }
    if args.access_policy is not None:
        options["access_policy"] = args.access_policy
    record = control_factory().add_root(args.path, **options)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_root_list(args) -> int:
    print(json.dumps(control_factory().list_roots(), ensure_ascii=False, indent=2))
    return 0


def cmd_root_show(args) -> int:
    print(json.dumps(control_factory().show_root(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_root_remove(args) -> int:
    print(json.dumps(control_factory().remove_root(args.identifier),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_root_match(args) -> int:
    record, relative = control_factory().match_root(args.path)
    print(json.dumps({"root": record, "relative_path": relative},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_file_resolve(args) -> int:
    control = control_factory()
    if args.absolute_path:
        if args.root or args.relative:
            raise UnifierError("pass either ROOT RELATIVE or --path, not both")
        reference = control.resolve_path(args.absolute_path, args.role)
    else:
        if not args.root or not args.relative:
            raise UnifierError("file resolve requires ROOT RELATIVE or --path")
        reference = control.resolve_file(args.root, args.relative, args.role)
    print(json.dumps(reference, ensure_ascii=False, indent=2))
    return 0


def cmd_file_copy(args) -> int:
    operation = control_factory().copy_file(
        args.source_root, args.source_relative,
        args.target_root, args.target_relative)
    print(json.dumps(operation, ensure_ascii=False, indent=2))
    return 0 if operation.get("complete") is True else 1


def cmd_file_move(args) -> int:
    operation = control_factory().move_file(
        args.source_root, args.source_relative,
        args.target_root, args.target_relative,
        cleanup_source=args.cleanup_source)
    print(json.dumps(operation, ensure_ascii=False, indent=2))
    return 0 if operation.get("complete") is True else 1


def cmd_file_status(args) -> int:
    control = control_factory()
    if args.operation:
        data = control.file_operation(args.operation)
    else:
        data = control.list_file_operations()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_file_recover(args) -> int:
    print(json.dumps(
        control_factory().recover_file_operations(args.operation),
        ensure_ascii=False, indent=2))
    return 0


def cmd_file_list(args) -> int:
    print(json.dumps(
        control_factory().list_file_operations(), ensure_ascii=False, indent=2))
    return 0


def cmd_res_sample(args) -> int:
    from unifier.resources import ResourceProbe
    data = ResourceProbe(service_factory()).sample(args.id)
    _emit_json(data, args.output)
    return 0


def cmd_res_series(args) -> int:
    from unifier.resources import ResourceProbe
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
    from unifier import shortcuts

    if not argv or argv[0] in {"-h", "--help"}:
        return shortcuts.help_command([])
    if argv[0] == "help":
        return shortcuts.help_command(argv[1:])
    if argv[0] in shortcuts.SHORTCUTS:
        return shortcuts.main(
            argv, context_factory=shortcut_context_factory)
    try:
        if argv[:2] == ["view", "_broker"]:
            from unifier.view_broker import main as broker_main
            return broker_main(argv[2:])
        if argv[:2] == ["view", "_fuse"]:
            from unifier.fuse_view import run_fuse
            return run_fuse(argv[2:])
        if argv[:2] == ["file", "_worker"]:
            args = file_worker_parser().parse_args(argv[2:])
            return cmd_file_worker(args)
        if argv[:2] == ["env", "run"]:
            return cmd_run(argv[2:])
        if argv[:2] == ["project", "_worker"]:
            args = project_worker_parser().parse_args(argv[2:])
            return session_factory().execute(args.session)
        if argv[:2] == ["project", "run"]:
            args = parse_project_run_args(argv[2:])
            if args is None:
                return 0
            return cmd_project_run(args)
        if argv[:2] == ["app", "_worker"]:
            # The supervised worker is an internal entry point. Parsing it here
            # keeps it out of `unifier app -h` while still accepting program argv.
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
    except (UnifierError, OSError, ValueError) as exc:
        print(f"unifier: {exc}", file=sys.stderr)
        return 1
