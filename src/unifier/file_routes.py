"""User-level project/handler routing for file plan and open.

This is the B-stage control facade. It connects one registered host project,
its accepted environment toolchain, stable handlers and a read-only FileRef.
Planning is a review snapshot; open resolves everything again before any
start or exec and never accepts a stale plan JSON as authority.
"""

import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

from unifier.control import FileControl
from unifier.errors import UnifierError
from unifier.handlers import (
    PROVIDERS,
    HandlerRegistry,
    build_handler,
    execution_fingerprint,
    render_argv,
    validate_file_argument,
)
from unifier.identity import validate_binding
from unifier.projects import (
    INTENTS,
    ProjectRegistry,
    build_project,
    validate_default_save_root,
)
from unifier.on_demand import exit_status
from unifier.registry import DataRootRegistry
from unifier.runtime import ExecResult
from unifier.file_access import resolve_static_mount, require_registered_permission
from unifier.file_access_store import AccessStore
from unifier.service import validate_env_id


FILE_WORKER_MARKER = "MULTILINUX_FILE_WORKER"


def _within(path: str, root: str) -> bool:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    return len(path_parts) >= len(root_parts) and path_parts[:len(root_parts)] == root_parts


def _options(share: dict) -> tuple:
    value = share.get("options", "")
    if not isinstance(value, str):
        raise UnifierError("environment shared_mounts options must be a string")
    return tuple(token.strip() for token in value.split(",") if token.strip())


def _resolve_container_path(mounts: list[dict], target: str) -> tuple[dict, str]:
    return resolve_static_mount(mounts, target)


def _access_scope_for(environment: dict, targets: dict[str, str]) -> dict:
    """Resolve named runtime paths through the longest static mount.

    Every target must be the same host object at the same absolute path in the
    container. A nested mount with a different host is masking and is refused.
    """
    shares = environment.get("shared_mounts")
    if not isinstance(shares, list):
        raise UnifierError("environment shared_mounts must be a list")
    listed = []
    for share in shares:
        if not isinstance(share, dict):
            raise UnifierError("environment shared_mounts entries must be objects")
        host = share.get("host")
        container = share.get("container")
        if (not isinstance(host, str) or not isinstance(container, str)
                or not host.startswith("/") or not container.startswith("/")):
            raise UnifierError("environment shared_mounts paths must be absolute strings")
        options = _options(share)
        listed.append({
            "host": os.path.normpath(host),
            "container": os.path.normpath(container),
            "options": ",".join(options),
            "same_absolute_path": os.path.normpath(host) == os.path.normpath(container),
        })

    mappings = {}
    used = {}
    for name, target in targets.items():
        target = os.path.normpath(target)
        mount, expected_host = _resolve_container_path(listed, target)
        if expected_host != target:
            raise UnifierError(
                f"container path {target!r} is shadowed by a different host mount "
                f"({mount['host']!r}); refusing the route")
        mapping = {"path": target, "mount": mount, "expected_host": expected_host,
                   "same_host_object": expected_host == target}
        mappings[name] = mapping
        used[(mount["host"], mount["container"], mount["options"])] = mount

    used_mounts = list(used.values())
    read_only = [("ro" in set(mount["options"].split(",")) if mount["options"] else False)
                 for mount in used_mounts]
    selected_name = "file" if "file" in mappings else (
        "cwd" if "cwd" in mappings else next(iter(mappings)))
    return {
        "mode": "environment-shared-static-mounts",
        "project_root": mappings["project_root"]["path"],
        "file_path": mappings.get("file", {}).get("path"),
        "cwd": mappings["cwd"]["path"],
        "mappings": mappings,
        "selected_mount": mappings[selected_name]["mount"],
        "used_mounts": used_mounts,
        "shared_mounts": listed,
        "runtime_read_only": all(read_only),
        "any_used_mount_read_only": any(read_only),
        "policy_enforcement": "routing-only",
        "policy_note": (
            "read-only is a routing decision; without an OS-level read-only mount "
            "it is not a filesystem write guarantee"),
        "warning": (
            "All applications in this environment share every static mount. "
            "This route is not per-file or per-application isolation."),
    }


def _access_scope(environment: dict, project_root: str, file_path: str, cwd: str) -> dict:
    return _access_scope_for(
        environment, {"project_root": project_root, "file": file_path, "cwd": cwd})


def project_session_scope(environment: dict, project_root: str, cwd: str) -> dict:
    """The shared-mount contract for a project session (no document file)."""
    return _access_scope_for(environment, {"project_root": project_root, "cwd": cwd})


class FileRoutes:
    def __init__(self, root: Path, service):
        self.root = Path(root)
        self.service = service
        self.roots = DataRootRegistry(self.root)
        self.files = FileControl(self.roots)
        self.projects = ProjectRegistry(self.root)
        self.handlers = HandlerRegistry(self.root)

    def _validate_default_save_root(self, root_record: dict, path: str) -> None:
        """Validate the save root against real path and every registered root.

        A symlinked directory or a nested registered root must not let the
        project's default save location bypass a stricter child policy.
        """
        validate_default_save_root(root_record, path)
        real = os.path.realpath(path)
        if real != os.path.normpath(path):
            raise UnifierError(
                "default_save_root must not resolve through symbolic links")
        matched, _relative = self.roots.match(real)
        if matched is None or matched["root_uuid"] != root_record["root_uuid"]:
            target = None if matched is None else matched["root_uuid"]
            raise UnifierError(
                "default_save_root enters a different registered data root"
                + (f" ({target})" if target else "")
                + "; choose a directory inside the project root")

    # ----- project and handler registration -------------------------------
    def add_project(self, root_uuid: str, environment_id: str, *, alias=None,
                    default_save_root: str | None = None,
                    access_policy: str = "read-write",
                    intent_handlers: dict | None = None) -> dict:
        root_record = self.roots.show(root_uuid)
        environment_id = validate_env_id(environment_id)
        record = self.service.store.load(environment_id)
        # Read-only ownership check: the project accepts one concrete instance,
        # and that instance must still match the persisted container id.
        info = self.service._require_owned(record)
        from unifier.identity import binding_for
        environment_binding = binding_for(record)
        if info.container_id != environment_binding["container_id"]:
            raise UnifierError(
                "environment container instance changed; refusing to create a project binding")
        if intent_handlers is not None and not isinstance(intent_handlers, dict):
            raise UnifierError("project intent_handlers must be an object")
        handlers = {}
        for intent, identifier in (intent_handlers or {}).items():
            if not isinstance(intent, str):
                raise UnifierError("project intent must be a string")
            if intent not in INTENTS:
                raise UnifierError(f"unknown project intent: {intent!r}")
            handler = self.handlers.show(identifier)
            if handler["intent"] != intent:
                raise UnifierError(
                    f"handler {handler['handler_uuid']} has intent {handler['intent']}, "
                    f"not {intent}")
            if handler["app_binding"] != environment_binding:
                raise UnifierError(
                    f"handler {handler['handler_uuid']} belongs to a different "
                    "environment instance")
            handlers[intent] = handler["handler_uuid"]
        save_root = (default_save_root if default_save_root is not None
                     else root_record["locator"])
        self._validate_default_save_root(root_record, save_root)
        project = build_project(
            root_record, record, alias=alias, default_save_root=save_root,
            access_policy=access_policy, intent_handlers=handlers,
            timestamp=self.service.clock())
        return self.projects.add(project)

    def list_projects(self) -> list[dict]:
        return self.projects.list()

    def show_project(self, identifier: str) -> dict:
        return self.projects.show(identifier)

    def remove_project(self, identifier: str) -> dict:
        return self.projects.remove(identifier)

    def add_handler(self, app_id: str, *, intent: str, save_capability: str,
                    provider: str = "environment-app", alias=None,
                    argv_template=None) -> dict:
        launch = self.service.show_app(app_id)
        # Read-only preflight: the app and its exact environment instance must
        # still be the ones recorded in the launch binding.
        self.service.validate_app_binding(app_id)
        handler = build_handler(
            launch, intent=intent, save_capability=save_capability,
            provider=provider, alias=alias, argv_template=argv_template,
            timestamp=self.service.clock())
        return self.handlers.add(handler)

    def list_handlers(self) -> list[dict]:
        return self.handlers.list()

    def show_handler(self, identifier: str) -> dict:
        return self.handlers.show(identifier)

    def remove_handler(self, identifier: str) -> dict:
        return self.handlers.remove(identifier)

    # ----- file planning and execution ------------------------------------
    def _resolve(self, path: str, intent: str) -> dict:
        if not isinstance(intent, str):
            raise UnifierError("file intent must be a string")
        if intent not in INTENTS:
            raise UnifierError(f"unknown file intent: {intent!r}")
        reference = self.files.resolve_path(path, role="document")
        root_record = self.roots.show(reference["root_uuid"])
        projects = self.projects.for_root(root_record["root_uuid"])
        if not projects:
            raise UnifierError(
                f"no project is registered for data root {root_record['root_uuid']}")
        if len(projects) > 1:
            raise UnifierError(
                f"data root {root_record['root_uuid']} has more than one project registration")
        project = projects[0]
        intent_handlers = project.get("intent_handlers")
        if not isinstance(intent_handlers, dict):
            raise UnifierError("project intent_handlers must be an object")
        handler_uuid = intent_handlers.get(intent)
        if not handler_uuid:
            raise UnifierError(
                f"project {project['project_uuid']} has no {intent} handler")
        handler = self.handlers.show(handler_uuid)
        if handler["intent"] != intent:
            raise UnifierError(
                f"handler {handler['handler_uuid']} does not serve intent {intent}")
        if handler["provider"] not in PROVIDERS:
            raise UnifierError(
                f"unsupported handler provider: {handler['provider']!r}")

        launch = self.service.show_app(handler["app_id"])
        # Read-only checks of app instance, actual container and binding.
        self.service._validate_launch_binding(launch)
        current_binding = launch.get("environment_binding")
        validate_binding(current_binding)
        if handler["app_binding"] != current_binding:
            raise UnifierError(
                "handler app binding changed; re-register the handler explicitly")
        if handler.get("app_instance_uuid") != launch.get("app_instance_uuid"):
            raise UnifierError(
                "application instance changed; re-register the handler explicitly")
        if handler["execution_fingerprint"] != execution_fingerprint(launch):
            raise UnifierError(
                "application execution configuration changed; re-register the handler")
        if project["environment_binding"] != current_binding:
            raise UnifierError(
                "project toolchain binding changed; re-register the project")
        environment = self.service.store.load(launch["environment_id"])
        self.service._require_record_binding(environment, project["environment_binding"])
        if environment.get("target") != project["toolchain"].get("target"):
            raise UnifierError("project toolchain target no longer matches the environment")

        absolute = os.path.join(root_record["locator"], reference["relative_path"])
        if os.path.normpath(absolute) != absolute or not _within(absolute, root_record["locator"]):
            raise UnifierError("resolved file path escaped the registered root")
        cwd = project["default_save_root"]
        self._validate_default_save_root(root_record, cwd)
        purpose = 'edit' if intent == 'edit' else 'read'
        policy = AccessStore(self.root).policy()
        policy.require(absolute, purpose)
        policy.require(cwd, purpose, directory=True)
        require_registered_permission(project, purpose)
        require_registered_permission(root_record, purpose)
        if intent == "edit":
            if project["access_policy"] != "read-write":
                raise UnifierError("project policy does not allow edit")
            if root_record["access_policy"] != "read-write":
                raise UnifierError("data root policy does not allow edit")

        scope = _access_scope(environment, root_record["locator"], absolute, cwd)
        selected_mount = scope["selected_mount"]
        if intent == "edit" and scope["any_used_mount_read_only"]:
            raise UnifierError(
                f"a used environment mount ({selected_mount['host']!r}) is read-only; "
                "edit is refused")
        policy_read_only = (project["access_policy"] == "read-only"
                            or root_record["access_policy"] == "read-only")
        if policy_read_only and not scope["runtime_read_only"]:
            raise UnifierError(
                "read-only policy requires every used environment mount to be "
                "OS read-only; refusing a read-only label over writable mounts")

        argv = render_argv(handler, launch, absolute)
        return {
            "schema_version": 1,
            "operation": "file.plan",
            "intent": intent,
            "provider": handler["provider"],
            "handler_uuid": handler["handler_uuid"],
            "handler_alias": handler.get("alias"),
            "project_uuid": project["project_uuid"],
            "project_alias": project.get("alias"),
            "root_uuid": root_record["root_uuid"],
            "environment_id": launch["environment_id"],
            "environment_uuid": current_binding["environment_uuid"],
            "generation": current_binding["generation"],
            "container_id": current_binding["container_id"],
            "app_id": launch["id"],
            "app_instance_uuid": launch.get("app_instance_uuid"),
            "execution_fingerprint": execution_fingerprint(launch),
            "environment_binding": dict(current_binding),
            "file_path": absolute,
            "relative_path": reference["relative_path"],
            "observed_version": reference["observed_version"],
            "role": reference["role"],
            "argv": argv,
            "cwd": cwd,
            "default_save_root": cwd,
            "save_capability": handler["save_capability"],
            "save_as_default": "unknown",
            "default_save_enforced": False,
            "save_as_default_note": (
                "cwd is set to the project default; application new-file Save As "
                "default is not verified by this plan"),
            "root_access_policy": root_record["access_policy"],
            "project_access_policy": project["access_policy"],
            "access_scope": scope,
            "file_ref": reference,
            "execution": {
                "mode": "argv-array",
                "shell": False,
                "on_demand": bool(launch.get("on_demand")),
                "revalidate_on_open": True,
            },
        }

    def plan(self, path: str, intent: str = "edit") -> dict:
        return self._resolve(path, intent)

    def _execute(self, context: dict) -> ExecResult:
        """Dispatch one already-resolved context with fixed expectations."""
        launch = self.service.show_app(context["app_id"])
        if launch.get("on_demand"):
            from unifier.on_demand import managed_exec
            return managed_exec(
                self.service, context["app_id"], context["argv"],
                workdir=context["cwd"],
                expected_app_instance_uuid=context["app_instance_uuid"],
                expected_execution_fingerprint=context["execution_fingerprint"])
        return self.service.exec_app(
            context["app_id"], context["argv"], capture=False, timeout=None,
            workdir=context["cwd"],
            expected_app_instance_uuid=context["app_instance_uuid"],
            expected_execution_fingerprint=context["execution_fingerprint"])

    def open(self, path: str, intent: str = "edit"):
        if os.environ.get(FILE_WORKER_MARKER) == "1":
            # A nested supervisor would lose the original worker's lease.
            return self.worker_open(path, intent)
        # Preflight is read-only. The detached worker resolves everything again
        # from path/intent; no plan JSON or resolved context crosses processes.
        context = self._resolve(path, intent)
        if self.service.show_app(context["app_id"]).get("on_demand"):
            return self._spawn_worker(path, intent)
        return self._execute(context)

    def worker_open(self, path: str, intent: str = "edit") -> ExecResult:
        """Trusted internal entry: resolve fresh, then hold the lease and exec."""
        context = self._resolve(path, intent)
        return self._execute(context)

    def _spawn_worker(self, path: str, intent: str) -> ExecResult:
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "tools/unifier.py"),
            "file", "_worker", "--path", path, "--intent", intent,
        ]
        from unifier.install import worker_environment
        environment = worker_environment(self.service.root, os.environ)
        environment[FILE_WORKER_MARKER] = "1"
        try:
            worker = subprocess.Popen(command, start_new_session=True, env=environment)
        except OSError as exc:
            raise UnifierError(f"cannot start the file supervisor: {exc}") from exc
        return ExecResult(exit_status(worker.wait()), "", "")
