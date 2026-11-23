"""Environment lifecycle above a runtime adapter.

The service owns the durable record. The runtime owns the container. A record
is written only after the container's distribution, kernel and package
inventory have been checked. A failed create removes the container it just
created and leaves no record.
"""

import contextlib
import datetime as dt
from dataclasses import replace
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

from unifier import package_recovery, package_transaction
# Keep the historical service.parse_desktop_exec import available to callers.
from unifier.app_entries import (
    desktop_exec_argv, desktop_menu_fields, package_entry_kind, parse_desktop_exec,
)
from unifier.camera_device import require_no_native_camera, resolve_camera, validate_saved_camera_mounts
from unifier.catalog import (
    plan_change,
    resolve_record_contract,
    resolve_target_contract,
    system_contract,
)
from unifier.desktop_clients import CLIENTS, client_packages, desktop_provisioning_failure
from unifier.errors import UnifierError
from unifier.file_chooser import (
    MODE_KEY, PORTAL_MODES, configured_environment, default_portal_profile,
    launch_argv, launch_environment, preflight_picker,
)
from unifier.portal_mount import (
    PORTAL_GUEST, resolve_portal_mount, validate_saved_portal_mounts,
    verify_active_portal_mount,
)
from unifier.operation_lock import management_lock
from unifier.package_inventory import InventoryObservation, normalize_inventory
from unifier.package_observation import summarize as summarize_observation, unavailable_host
from unifier.compatibility import (
    compatibility_mode, observe_host, relationship_plan, require_relationship,
)
from unifier.handlers import execution_fingerprint as app_execution_fingerprint
from unifier.identity import (
    binding_for,
    check_record_header,
    identity,
    schema_version,
    stamp_new_identity,
    validate_binding,
    validate_migratable_record,
)
from unifier.install import ROOT_ENVIRONMENT, code_root
from unifier.input_method import apply_input_method, validate_input_method
from unifier.packages import (
    INCOMING_DIR,
    HostObserver,
    driver_for,
    entry_changes,
    inventory_argv,
    inventory_has,
    local_package_path,
    native_env,
    parse_package_files,
    query_argv,
    validate_package_name,
)
from unifier.desktop import (
    MIME_TYPE,
    HostDesktop,
    add_mime_association,
    desktop_id,
    discover_session,
    remove_mime_association,
    render_desktop_entry,
    render_mime_xml,
    restore_xauthority_mount,
    validate_app_id,
    validate_desktop_session,
)
from unifier.share_label import (
    effective_policy as effective_share_label_policy,
    validate_policy as validate_share_label_policy,
    volume_options as share_volume_options,
)
from unifier.render_node import (
    FULL_CID, STOPPED, RenderNodeAccessError, render_node_identity, require_no_native_drm,
    require_stopped, resolve_render_node, validate_saved_mounts,
)
from unifier.runtime import (
    APPLICATION_PYTHON_CHECK,
    APPLICATION_PYTHON_MISSING,
    ROLE_ENVIRONMENT,
    ContainerState,
    CreateSpec,
    ExecResult,
    PodmanRuntime,
    container_name,
    project_label,
)
from unifier.store import EnvironmentStore
from unifier.environment_contract import ENV_ID, validate_env_id
from unifier.view_state import assert_no_active_views
from unifier.wayland import plan_refresh


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
            and bool(left) and bool(right)
            and left.get("binding") == right.get("binding")
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
            "binding_before": left.get("binding"),
            "binding_after": right.get("binding"),
            "observation_before": left.get("observation"),
            "observation_after": right.get("observation"),
        }
    return compared


def _hides_system_tree(path: str) -> bool:
    if path in {"/", "/home"}:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in SYSTEM_PREFIXES)


def parse_os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values



class EnvironmentService:
    def __init__(self, root: Path, runtime, config: dict, lock: dict, *,
                 host_kernel: str | None = None, clock=None, uid: int | None = None,
                 gid: int | None = None, host_observer=None, log=None,
                 host_desktop=None, cache_runner=None, host_facts: dict | None = None):
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
        self.host_facts = host_facts
        self.log = log or _log_progress

    def compatibility_plan(self, target_name: str) -> dict:
        compatibility_mode(self.config)
        facts = self.host_facts if self.host_facts is not None else observe_host()
        raw = self.config.get("targets", {}).get(target_name)
        return relationship_plan(facts, target_name, raw)

    def _require_host_compatibility(self, record: dict) -> dict:
        resolve_record_contract(record)
        return require_relationship(self.compatibility_plan(record["target"]))

    def create(self, target_name: str, env_id: str | None, shares: list[str],
               network: str = "none", desktop: bool = False, session: dict | None = None,
               user_namespace: str = "rootless-default", devices: list[str] | None = None,
               portals: bool | None = False, gpu: bool = False, ipc: str | None = None,
               share_label_policy: str = "preserve-host", *, cuda_device: str | None = None,
               provision_desktop_clients: bool = False) -> dict:
        # Refuse policy violations before creating even a lock or state directory.
        resolve_target_contract(target_name, self._target(target_name))
        require_relationship(self.compatibility_plan(target_name))
        env_id = validate_env_id(env_id or target_name)
        with self._creation_lock():
            record = self._create_locked(
                target_name, env_id, shares, network, desktop, session,
                user_namespace, devices, portals, gpu, ipc, share_label_policy,
                cuda_device=cuda_device, provision_desktop_clients=provision_desktop_clients)
        if desktop_provisioning_failure(record):
            # Package writes take the same management lock as creation. The
            # new record must be committed and the creation lock released first.
            try:
                self.repair_desktop_clients(env_id, expected_binding=binding_for(record))
                updated = self.store.load(env_id)
                self._require_record_binding(updated, binding_for(record))
                return updated
            except Exception as exc:
                # Keep the durable pending marker if provisioning could not
                # reach its outcome write, including when lock admission fails.
                return {**record, "desktop_client_provisioning": {
                    **record["desktop_client_provisioning"], "detail": str(exc)[-2000:]}}
        return record

    def _create_locked(self, target_name: str, env_id: str, shares: list[str],
                       network: str, desktop: bool, session: dict | None,
                       user_namespace: str, devices: list[str] | None,
                       portals: bool | None, gpu: bool, ipc: str | None,
                       share_label_policy: str, *, cuda_device: str | None = None,
                       provision_desktop_clients: bool = False) -> dict:
        spec, target, host_relationship, desktop_session = self._prepare_create_locked(
            target_name, env_id, shares, network, desktop, session, user_namespace,
            devices, portals, gpu, ipc, share_label_policy, cuda_device=cuda_device)
        existing = self.runtime.inspect(spec.name)
        if existing is not None:
            raise UnifierError(
                f"container {spec.name} already exists; this project will not replace it")
        self.runtime.create(spec)
        return self._finish_create(
            spec, target_name, target, host_relationship, desktop_session,
            provision_desktop_clients=provision_desktop_clients)

    def resume_create(self, target_name: str, env_id: str | None, shares: list[str], *,
                      expected_container_id: str, network: str = "none",
                      user_namespace: str = "rootless-default", devices: list[str] | None = None,
                      ipc: str | None = None, share_label_policy: str = "preserve-host") -> dict:
        """Finish an explicitly identified, never-started headless creation.

        This does not adopt a running/previously used container, recreate it,
        or manufacture identity/inventory from caller-supplied metadata.
        """
        if not isinstance(expected_container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_container_id):
            raise UnifierError("resume-create requires one complete container ID")
        resolve_target_contract(target_name, self._target(target_name))
        require_relationship(self.compatibility_plan(target_name))
        env_id = validate_env_id(env_id or target_name)
        with self._creation_lock():
            spec, target, relationship, _ = self._prepare_create_locked(
                target_name, env_id, shares, network, False, None, user_namespace,
                devices, False, False, ipc, share_label_policy)
            # A partial previous registration needs separate reconciliation;
            # this entry never overwrites an existing record or inventory.
            if (os.path.lexists(self.store.path(env_id))
                    or os.path.lexists(self.store.inventory_path(env_id))):
                raise UnifierError("resume-create refuses existing registration files")
            if (self.store.transactions_for(env_id) or self.store.launches_for(env_id)
                    or os.path.lexists(self.root / '.lab/leases' / (env_id + '.json'))):
                raise UnifierError("resume-create refuses existing leases or operation history")
            observed = self.runtime.verify_created(spec, expected_container_id)
            if (observed.container_id != expected_container_id or observed.running
                    or observed.status != 'created' or observed.pid != 0):
                raise UnifierError("resume-create runtime did not verify the unused container")
            return self._finish_create(spec, target_name, target, relationship, None,
                                       expected_container_id=expected_container_id)

    def _prepare_create_locked(self, target_name: str, env_id: str, shares: list[str],
                               network: str, desktop: bool, session: dict | None,
                               user_namespace: str, devices: list[str] | None,
                               portals: bool | None, gpu: bool, ipc: str | None,
                               share_label_policy: str, *, cuda_device: str | None = None):
        target = self._target(target_name)
        resolve_target_contract(target_name, target)
        host_relationship = require_relationship(self.compatibility_plan(target_name))
        if self.store.exists(env_id):
            raise UnifierError(f"environment {env_id} already exists")
        self._require_unique_target(target_name)
        if network not in {"none", "pasta"}:
            raise UnifierError("environment network must be none or pasta")
        if user_namespace not in {"rootless-default", "keep-id"}:
            raise UnifierError("user namespace must be rootless-default or keep-id")
        share_label_policy = validate_share_label_policy(share_label_policy)
        if session is not None and not desktop:
            raise UnifierError("a session snapshot requires --desktop")
        if (portals or gpu) and not desktop:
            raise UnifierError("automatic GPU selection and desktop portals require --desktop")
        cuda = None
        if cuda_device is not None:
            if desktop or devices or ipc == "host" or user_namespace != "rootless-default":
                raise UnifierError("CUDA requires a headless rootless-default environment with private IPC and no other device requests")
            from unifier.cuda_device import resolve_cuda_device
            cuda = resolve_cuda_device(cuda_device)
        for device in devices or []:
            # Render nodes support offscreen graphics/compute without exposing
            # the host's display, audio, IPC or portal session. Primary DRM
            # nodes and cameras retain their existing desktop boundary.
            if not desktop and re.fullmatch(r"/dev/(video[0-9]+|dri/card[0-9]+)", device):
                raise UnifierError("camera and primary DRM devices require --desktop")
            if not re.fullmatch(r"(/dev/video[0-9]+|/dev/dri/(card|renderD)[0-9]+)", device) or not (
                os.path.exists(device) and stat.S_ISCHR(os.stat(device).st_mode)
            ):
                raise UnifierError("device must be an existing /dev/videoN camera or /dev/dri device")
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
                inside = PORTAL_GUEST
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
        image = self._locked_reference(target_name)
        mounts = tuple(self._shares(shares))
        if desktop_session is not None and portals is None:
            try:
                desktop_session["portal_proxy"] = self._prepare_managed_portal_bridge(
                    env_id, ((host, guest, True, "bind")
                             for host, guest in (*mounts, *session_mounts)))
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot configure Portal bridge for {env_id}: {exc}") from exc
        if cuda is not None:
            from unifier.cuda_device import CUDA_DIRECTORY
            cuda_path = Path(CUDA_DIRECTORY)
            if any(cuda_path.is_relative_to(destination) or Path(destination).is_relative_to(cuda_path)
                   for _, destination in mounts):
                raise UnifierError("a share overlaps the CUDA driver directory")
        from unifier.file_access_store import AccessStore
        AccessStore(self.root).check_mounts(
            [{'host': host, 'container': guest} for host, guest in mounts])
        spec = CreateSpec(
            name=container_name(self.root, env_id),
            env_id=env_id,
            project=project_label(self.root),
            image=image,
            mounts=mounts,
            share_label_policy=share_label_policy,
            network=network,
            session_mounts=session_mounts,
            selinux_label=selinux_label,
            user_namespace=user_namespace,
            devices=tuple(effective_devices),
            ipc=ipc or ("host" if desktop else "private"),
            cuda=cuda,
        )
        return spec, target, host_relationship, desktop_session

    def _finish_create(self, spec: CreateSpec, target_name: str, target: dict,
                       host_relationship: dict, desktop_session: dict | None, *,
                       expected_container_id: str | None = None,
                       provision_desktop_clients: bool = False) -> dict:
        # Recovery addresses every native action by full CID. Keep the declared
        # name in the durable record, and never let a name replacement redirect
        # start/exec/stop while completing an existing container.
        execution = replace(spec, name=expected_container_id) if expected_container_id else spec
        execution_identity = None
        def owned():
            value = self._require_owned_record(execution.name, spec.project, spec.env_id)
            if expected_container_id and value.container_id != expected_container_id:
                raise UnifierError("resume-create container identity changed")
            if (expected_container_id and execution_identity is not None and value.running
                    and (value.pid, value.started_at) != execution_identity):
                raise UnifierError("resume-create execution identity changed")
            return value

        try:
            if expected_container_id:
                # Repeat the native unused-layout check immediately before
                # start; a used/exited object cannot become resumable again.
                self.runtime.verify_created(spec, expected_container_id)
            self.runtime.start(execution.name, project=spec.project, env_id=spec.env_id)
            if expected_container_id:
                started = owned()
                if not started.running or not started.pid or not started.started_at:
                    raise UnifierError("resume-create could not bind the started execution")
                execution_identity = (started.pid, started.started_at)
            identity = self._read_identity(execution)
            self._validate_identity(target_name, target, identity)
            inventory = self._read_inventory(execution, target_name)
            if expected_container_id and execution_identity is not None:
                owned()
            self.runtime.stop(execution.name, project=spec.project, env_id=spec.env_id)
            info = owned()
            if expected_container_id and (info.running or info.pid != 0):
                raise UnifierError("resume-create could not verify the stopped container")
            record = self._record(spec, target_name, target, spec.image, identity, inventory, info,
                                  desktop_session)
            record["compatibility_at_creation"] = host_relationship
            bridge = (desktop_session or {}).get("portal_proxy") or {}
            if (provision_desktop_clients and spec.network == "pasta"
                    and bridge.get("managed_mount") is True):
                record["desktop_client_provisioning"] = {"phase": "pending"}
            self.store.save(record, inventory)
            return record
        except Exception:
            if expected_container_id and execution_identity is not None:
                # Retain the original object and evidence. Stop only the same
                # verified CID; an ownership mismatch never widens cleanup.
                with contextlib.suppress(Exception):
                    if owned().running:
                        self.runtime.stop(execution.name, project=spec.project, env_id=spec.env_id)
            elif not expected_container_id:
                with contextlib.suppress(Exception):
                    self.runtime.remove(spec.name, project=spec.project, env_id=spec.env_id)
            raise

    def _require_unique_target(self, target_name: str) -> None:
        """Retained and legacy records also reserve their target in this root."""
        selection_path = self.root / "install-state.json"
        try:
            selection = json.loads(selection_path.read_text()) if selection_path.exists() else {"targets": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise UnifierError(f"cannot verify adopted target uniqueness: {exc}") from exc
        if not isinstance(selection, dict) or not isinstance(selection.get("targets"), dict):
            raise UnifierError("cannot verify adopted target uniqueness: invalid installation record")
        adopted = selection["targets"].get(target_name, {})
        if not isinstance(adopted, dict):
            raise UnifierError("cannot verify adopted target uniqueness: invalid target record")
        if adopted.get("source") == "adopted":
            raise UnifierError(
                f"target {target_name} already has an adopted environment; "
                "only one environment per target is allowed in this installation")
        try:
            records = self.store.all()
        except (OSError, json.JSONDecodeError) as exc:
            raise UnifierError(f"cannot verify environment target uniqueness: {exc}") from exc
        if any(not isinstance(record, dict) or not isinstance(record.get("target"), str)
               for record in records):
            raise UnifierError("cannot verify environment target uniqueness: invalid record")
        conflicts = [str(record.get("id", "unknown")) for record in records
                     if record["target"] == target_name]
        if conflicts:
            raise UnifierError(
                f"target {target_name} already has an environment: {', '.join(conflicts)}; "
                "only one environment per target is allowed in this installation")

    @contextlib.contextmanager
    def _creation_lock(self):
        """Serialize validation through durable record creation across processes."""
        with self.package_coordination_lock():
            with self._creation_record_lock():
                yield

    @contextlib.contextmanager
    def _creation_record_lock(self):
        path = self.root / ".lab/state/operations/create.lock"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def list_summaries(self) -> list[dict]:
        summaries = []
        for record in self.store.all():
            view = self._live(record)
            summaries.append({
                "id": view["id"],
                "environment_uuid": view.get("environment_uuid"),
                "generation": view.get("generation"),
                "target": view["target"],
                "distribution_id": view["distribution_id"],
                "version_track": view["version_track"],
                "runtime_state": view["runtime_state"],
                "share_label_policy": view.get("share_label_policy"),
                "writable_revision": view["writable_revision"],
                "base_image_digest": view["base_image"]["manifest_digest"],
                "kernel_matches_current_host": view["kernel_matches_current_host"],
                "host_family": view["compatibility"].get("host", {}).get("family"),
                "container_family": view["compatibility"].get("container_family"),
                "host_relationship": view["compatibility"].get("relationship"),
                "compatibility_allowed": view["compatibility"].get("allowed", False),
            })
        return summaries

    def inspect(self, env_id: str) -> dict:
        return self._live(self.store.load(validate_env_id(env_id)))

    def hardware(self, env_id: str, *, evidence=()) -> dict:
        """Describe registered hardware and check its binding without starting it."""
        from unifier.hardware_report import hardware_report, registered_render_nodes

        record = self.store.load(validate_env_id(env_id))
        try:
            expected = binding_for(record)
            info = self._require_owned(record)
        except (KeyError, TypeError, UnifierError):
            return hardware_report(record, evidence=evidence)
        observed = {**expected, "container_id": info.container_id}
        observations = []
        device_errors = {}
        paths = registered_render_nodes(record["runtime"]) if expected == observed else ()
        for path in paths:
            key = "render-node", path
            try:
                node = resolve_render_node(path)
            except FileNotFoundError:
                device_errors[key] = "device-missing"
            except (RenderNodeAccessError, PermissionError):
                device_errors[key] = "device-unreadable"
            except (OSError, ValueError):
                device_errors[key] = "device-unobserved"
            else:
                try:
                    identity = render_node_identity(node)
                    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                except (OSError, ValueError):
                    device_errors[key] = "device-unobserved"
                    continue
                observations.append({
                    "kind": "render-node", "selector": path, "boot_id": boot_id,
                    "device_identity": identity,
                    "backend": "kernel-drm", "backend_version": os.uname().release,
                })
        return hardware_report(record, observed_binding=observed, observations=observations,
                               evidence=evidence, device_errors=device_errors)

    def desktop_integration(self, env_id: str) -> dict:
        from unifier.host_portal import describe_host_portal

        record = self.store.load(validate_env_id(env_id))
        desktop = record.get("desktop")
        bridge = (desktop or {}).get("portal_proxy")
        bridge_mode = "headless"
        if desktop:
            bridge_mode = "disabled"
            if bridge:
                bridge_mode = "managed" if bridge.get("managed_mount") is True else "legacy"
        return {
            "environment_id": record["id"],
            "target": record["target"],
            "portal_bridge": bridge_mode,
            "host_portal": describe_host_portal() if desktop else None,
            "client_packages": client_packages(record["target"], CLIENTS),
        }

    def install_desktop_clients(self, env_id: str, clients) -> dict:
        if not clients:
            raise UnifierError("select at least one desktop client")
        return self._package_transaction(env_id, "install", [], desktop_clients=tuple(clients))

    def repair_desktop_clients(self, env_id: str, *, expected_binding: dict | None = None) -> dict:
        """Provision the desktop baseline for the recorded instance in place."""
        env_id = validate_env_id(env_id)
        if expected_binding is None:
            expected_binding = binding_for(self.store.load(env_id))
        with self.package_coordination_lock(), self.package_operation_lock(env_id):
            record = self.store.load(env_id)
            self._require_record_binding(record, expected_binding)
            self._require_recovered_packages(env_id)
            txn = self._package_transaction_locked(env_id, "install", [], desktop_clients=CLIENTS)
            if desktop_provisioning_failure(record):
                with self._lifecycle_lock(env_id):
                    current = self.store.load(env_id)
                    self._require_record_binding(current, expected_binding)
                    current["desktop_client_provisioning"] = {
                        "phase": txn["phase"],
                        "transaction_id": txn["id"],
                        "detail": txn.get("native_error") or txn.get("failure_phase"),
                        "exit_code": txn.get("exit_code"),
                        "recovery_required": bool(txn.get("recovery_required")),
                    }
                    self.store.save(current, None)
            return txn

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
        if "share_label_policy" in desired:
            desired["share_label_policy"] = validate_share_label_policy(
                desired["share_label_policy"])
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
        plan = plan_change(record, desired or None)
        plan["environment_uuid"] = record.get("environment_uuid")
        plan["generation"] = record.get("generation")
        plan["compatibility"] = self.compatibility_plan(desired.get("target", record["target"]))
        if not plan["compatibility"]["allowed"]:
            plan["action"] = "refused"
            plan["reason"] = "; ".join(plan["compatibility"]["reasons"])
        return plan

    def migrate_environment(self, env_id: str) -> dict:
        """Explicitly add versioned identity to one valid legacy record.

        This is deliberately not called by list/inspect. It never asks the
        runtime for anything, so it cannot start, stop or create a container.
        The per-record flock makes concurrent migrations agree on one UUID;
        the original bytes are saved once under ``environments/backups/``.

        Validation happens once before the lock is created, so a malformed
        record leaves no lock or backup behind. The same checks run again under
        the lock to close the preflight race.
        """
        env_id = validate_env_id(env_id)

        def preflight(record: dict):
            check_record_header(record, env_id)
            version = schema_version(record)
            # The same contract gate used by planning, creation and drivers.
            resolve_record_contract(record)
            existing = identity(record)
            if existing is not None:
                return version, existing
            if version != 1:
                # A schema-2 record without identity is not a legacy record;
                # issuing a UUID here would hide a malformed write.
                raise UnifierError(
                    f"environment schema_version {version} has no identity; "
                    "only legacy schema-1 records can be migrated")
            validate_migratable_record(record, env_id)
            return version, None

        record = self.store.load(env_id)
        _version, existing = preflight(record)
        if existing is not None:
            return {
                "environment_id": env_id,
                "status": "already-migrated",
                "environment_uuid": existing[0],
                "generation": existing[1],
                "backup": None,
            }

        lock_path = self.store.identity_lock_path(env_id)
        with self.package_coordination_lock():
            return self._migrate_identity_locked(env_id, lock_path, preflight)

    def _migrate_identity_locked(self, env_id, lock_path, preflight):
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            record = self.store.load(env_id)
            _version, existing = preflight(record)
            if existing is not None:
                return {
                    "environment_id": env_id,
                    "status": "already-migrated",
                    "environment_uuid": existing[0],
                    "generation": existing[1],
                    "backup": None,
                }
            backup = self.store.backup_environment(env_id)
            stamp_new_identity(record)
            self.store.save(record, None)
            return {
                "environment_id": env_id,
                "status": "migrated",
                "environment_uuid": record["environment_uuid"],
                "generation": record["generation"],
                "backup": str(backup),
            }
        finally:
            os.close(descriptor)

    def export(self, env_id: str, output: Path) -> dict:
        view = self.inspect(env_id)
        output = Path(output)
        if not output.is_absolute():
            output = Path.cwd() / output
        self._write_export(output, view)
        return view

    def _require_start_expected(self, record: dict, expected_app: dict | None,
                                expected_binding: dict | None):
        """Check app instance/fingerprint and environment binding before start.

        Returns the binding that later runtime checks must use. Reading the
        launch again on every call is intentional: callers may have waited for
        a stopping container while the app record changed.
        """
        if expected_app is not None:
            if not isinstance(expected_app, dict):
                raise UnifierError("expected_app must be an object")
            app_id = expected_app.get("app_id")
            launch = self.show_app(app_id)
            if launch.get("environment_id") != record.get("id"):
                raise UnifierError(
                    "application no longer belongs to the requested environment")
            self._require_app_execution(
                launch,
                app_instance_uuid=expected_app.get("app_instance_uuid"),
                execution_fingerprint=expected_app.get("execution_fingerprint"))
            launch_binding = launch.get("environment_binding")
            validate_binding(launch_binding)
            if expected_binding is None:
                expected_binding = launch_binding
            elif expected_binding != launch_binding:
                raise UnifierError(
                    "application environment binding changed before start")
        if expected_binding is not None:
            self._require_record_binding(record, expected_binding)
        return expected_binding

    def _portal_directory(self, env_id: str) -> Path:
        return self.root.resolve() / ".lab" / "desktop" / env_id

    def _create_portal_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self.root.resolve(), flags)
        try:
            for component in directory.relative_to(self.root.resolve()).parts:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                metadata = os.fstat(fd)
                if metadata.st_uid != self.uid or metadata.st_mode & 0o022:
                    raise ValueError("Portal bridge directory must be under host-owned directories")
        finally:
            os.close(fd)

    def _resolve_managed_portal_mount(self, record):
        directory = self._portal_directory(record["id"])
        settings = record["desktop"]["portal_proxy"]
        if settings.get("directory") != str(directory) or settings.get("container") != PORTAL_GUEST:
            raise ValueError("Portal bridge path does not match the registered environment")
        return resolve_portal_mount(directory, self.uid)

    def _prepare_managed_portal_bridge(self, env_id: str, mounts) -> dict:
        validate_saved_portal_mounts(mounts)
        directory = self._portal_directory(env_id)
        self._create_portal_directory(directory)
        resolve_portal_mount(directory, self.uid)
        return {"directory": str(directory), "container": PORTAL_GUEST, "managed_mount": True}

    @staticmethod
    def _require_portal_bridge_environment(record, info):
        runtime = record["runtime"]
        if (not record.get("desktop") or runtime.get("backend") != "podman"
                or runtime.get("rootless") is not True):
            raise ValueError("Portal bridge configuration requires a rootless Podman desktop environment")
        if (not FULL_CID.fullmatch(info.container_id) or info.running or info.pid not in (0, None)
                or info.status not in STOPPED):
            raise ValueError("Portal bridge configuration requires a stopped container with a full ID")
        if info.container_id != runtime.get("container_id"):
            raise ValueError("container identity differs from the recorded container ID")

    def configure_portal_bridge(self, env_id: str, mode: str | None = None) -> bool:
        """Configure the next start of an existing desktop container."""
        from unifier.on_demand import LaunchGroup

        env_id = validate_env_id(env_id)
        if mode is None:
            desktop = self.store.load(env_id).get("desktop") or {}
            return bool(desktop.get("portal_proxy"))
        if mode not in ("enable", "disable"):
            raise UnifierError(f"unknown Portal bridge mode: {mode}")
        group = LaunchGroup(self, env_id)
        with (management_lock(self.root), group.locked(),
              self.package_operation_lock(env_id, blocking=False),
              self._lifecycle_lock(env_id)):
            state = group.read()
            if state["leases"] or state["bridge"] is not None:
                raise UnifierError(f"cannot configure Portal bridge for {env_id} while it has leases or bridge state")
            assert_no_active_views(self.root, env_id)
            record = self.store.load(env_id)
            info = self._require_owned(record)
            try:
                self._require_portal_bridge_environment(record, info)
                desktop = record["desktop"]
                bridge = desktop.get("portal_proxy")
                if bridge and bridge.get("managed_mount") is not True:
                    if mode == "disable":
                        raise ValueError("the Portal bridge was mounted at creation and cannot be removed in place")
                    return True
                if mode == "enable":
                    if bridge:
                        mount = self._resolve_managed_portal_mount(record)
                        validate_saved_portal_mounts(info.mounts, mount.path)
                        return True
                    desktop["portal_proxy"] = self._prepare_managed_portal_bridge(env_id, info.mounts)
                elif bridge:
                    apps = [launch["id"] for launch in self.store.launches_for(env_id)
                            if launch["env"].get(MODE_KEY) in PORTAL_MODES]
                    if apps:
                        raise ValueError(
                            "reset the following applications with app file-chooser APP native first: "
                            + ", ".join(apps))
                    del desktop["portal_proxy"]
                else:
                    return False
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot configure Portal bridge for {env_id}: {exc}") from exc
            record["updated_at"] = self.clock()
            self.store.save(record, None)
        return mode == "enable"

    def configure_render_node(self, env_id: str, path: str | None) -> str | None:
        """Record one optional DRM render bind for a stopped desktop environment."""
        from unifier.on_demand import LaunchGroup

        env_id = validate_env_id(env_id)
        group = LaunchGroup(self, env_id)
        with (group.locked(),
              self.package_operation_lock(env_id, blocking=False),
              self._lifecycle_lock(env_id)):
            if group.read()["leases"]:
                raise UnifierError(f"cannot configure render node for {env_id} while it has active leases")
            assert_no_active_views(self.root, env_id)
            record = self.store.load(env_id)
            runtime = record["runtime"]
            info = self._require_owned(record)
            try:
                self._require_render_node_environment(record, info)
                current = runtime.get("render_node")
                selection = None
                if path is not None:
                    selection = render_node_identity(resolve_render_node(path))
                    validate_saved_mounts(info.mounts, current if current == path else None)
                elif current is not None:
                    validate_saved_mounts(info.mounts, current)
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot configure render node for {env_id}: {exc}") from exc
            if current != path or selection != runtime.get("render_node_identity"):
                if path is None:
                    runtime.pop("render_node", None)
                    runtime.pop("render_node_identity", None)
                else:
                    runtime["render_node"] = path
                    runtime["render_node_identity"] = selection
                record["updated_at"] = self.clock()
                self.store.save(record, None)
        return path

    def configure_camera(self, env_id: str, path: str | None) -> str | None:
        """Select or clear one camera for all apps in a stopped desktop guest."""
        from unifier.on_demand import LaunchGroup

        env_id = validate_env_id(env_id)
        group = LaunchGroup(self, env_id)
        with (management_lock(self.root), group.locked(),
              self.package_operation_lock(env_id, blocking=False), self._lifecycle_lock(env_id)):
            state = group.read()
            if state["leases"] or state["bridge"] is not None:
                raise UnifierError(f"cannot configure camera for {env_id} while it has leases or bridge state")
            assert_no_active_views(self.root, env_id)
            record = self.store.load(env_id)
            runtime = record["runtime"]
            info = self._require_owned(record)
            current = runtime.get("camera")
            try:
                self._require_camera_environment(record, info)
                validate_saved_camera_mounts(info.mounts, (current or {}).get("path"))
                selection = resolve_camera(path).selection if path is not None else None
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot configure camera for {env_id}: {exc}") from exc
            if path is None:
                if current is None:
                    return None
                del runtime["camera"]
            else:
                candidate = {"path": path, "selection": selection}
                if candidate == current:
                    return path
                runtime["camera"] = candidate
            record["updated_at"] = self.clock()
            self.store.save(record, None)
        return path

    @staticmethod
    def _require_camera_environment(record, info):
        runtime = record["runtime"]
        if (not record.get("desktop") or runtime.get("backend") != "podman"
                or runtime.get("rootless") is not True or runtime.get("cuda")):
            raise ValueError("camera configuration requires a rootless Podman desktop environment")
        if (not FULL_CID.fullmatch(info.container_id) or info.running or info.pid not in (0, None)
                or info.status not in STOPPED or info.bind_mounts is None):
            raise ValueError("camera configuration requires a stopped container with a full ID and known mounts")
        if info.container_id != runtime.get("container_id"):
            raise ValueError("container identity differs from the recorded container ID")
        require_no_native_camera(info)

    @staticmethod
    def _require_render_node_environment(record, info):
        runtime = record["runtime"]
        if (not record.get("desktop") or runtime.get("backend") != "podman"
                or runtime.get("rootless") is not True):
            raise ValueError("render-node configuration requires a rootless Podman desktop environment")
        require_stopped(info)
        if info.container_id != runtime.get("container_id"):
            raise ValueError("container identity differs from the recorded container ID")
        if runtime.get("devices") or runtime.get("cuda"):
            raise ValueError("container already has devices configured at creation")
        require_no_native_drm(info)

    def start(self, env_id: str, *, expected_binding: dict | None = None,
              expected_app: dict | None = None) -> dict:
        """Start one environment.

        ``expected_binding``/``expected_app`` are supplied by application
        launch paths so identity, app instance and execution configuration are
        checked under the lifecycle lock and again after a wait for a stopping
        container, immediately before any runtime mutation.
        """
        env_id = validate_env_id(env_id)
        if expected_binding is not None:
            validate_binding(expected_binding)
        if expected_app is not None and not isinstance(expected_app, dict):
            raise UnifierError("expected_app must be an object")
        self._require_host_compatibility(self.store.load(env_id))
        with self._lifecycle_lock(env_id):
            assert_no_active_views(self.root, env_id)
            record = self.store.load(env_id)
            self._require_host_compatibility(record)
            expected_binding = self._require_start_expected(
                record, expected_app, expected_binding)
            info = self._wait_while_stopping(record)

            # The wait can be long enough for the app/env record to change.
            # Never start from the pre-wait record or save it back over newer
            # state. Re-read and repeat every expectation first.
            record = self.store.load(env_id)
            self._require_host_compatibility(record)
            expected_binding = self._require_start_expected(
                record, expected_app, expected_binding)
            info = self._require_owned(record)
            if expected_binding is not None and info.container_id != expected_binding["container_id"]:
                raise UnifierError(
                    f"refusing to start {env_id}: its container instance no longer "
                    "matches the application binding; re-register the application")
            if not info.running:
                runtime = record["runtime"]
                try:
                    wayland = plan_refresh(record.get("desktop"), mounts=info.mounts, uid=self.uid)
                    options = {"wayland": wayland} if wayland is not None else {}
                    if "render_node" in runtime:
                        self._require_render_node_environment(record, info)
                        render_node = resolve_render_node(runtime["render_node"])
                        if ("render_node_identity" in runtime
                                and render_node_identity(render_node) != runtime["render_node_identity"]):
                            raise ValueError(
                                "the physical DRM identity changed; select it again with env render-node")
                        validate_saved_mounts(info.mounts, render_node.path)
                        options["render_node"] = render_node
                    bridge = (record.get("desktop") or {}).get("portal_proxy") or {}
                    if bridge.get("managed_mount") is True:
                        self._require_portal_bridge_environment(record, info)
                        portal_mount = self._resolve_managed_portal_mount(record)
                        validate_saved_portal_mounts(info.mounts, portal_mount.path)
                        options["portal_mount"] = portal_mount
                    if "camera" in runtime:
                        self._require_camera_environment(record, info)
                        selection = runtime["camera"]
                        camera = resolve_camera(selection["path"])
                        if camera.selection != selection["selection"]:
                            raise ValueError("the configured camera changed; select it again with env camera")
                        validate_saved_camera_mounts(info.mounts, camera.path)
                        options["camera"] = camera
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise UnifierError(f"cannot start {env_id}: {exc}") from exc
                restore_xauthority_mount(record, uid=self.uid)
                target = info.container_id if options else runtime["container_name"]
                self.runtime.start(target, project=runtime["project"], env_id=record["id"], **options)
                kernel = self._optional_capture(record, ["uname", "-r"])
                # Merge into the newest record rather than writing the record
                # captured before the wait/start.
                record = self.store.load(env_id)
                if expected_app is not None or expected_binding is not None:
                    self._require_start_expected(record, expected_app, expected_binding)
                if kernel:
                    record["observed"]["kernel"] = kernel.strip()
                record["runtime_state"] = "running"
                record["updated_at"] = self.clock()
                self.store.save(record, None)
        return self.inspect(env_id)

    def stop(self, env_id: str, *, package_lock_held: bool = False) -> dict:
        env_id = validate_env_id(env_id)
        guard = (contextlib.nullcontext() if package_lock_held
                 else self.package_operation_lock(env_id, blocking=False))
        with guard:
            return self._stop_locked(env_id)

    def _stop_locked(self, env_id: str) -> dict:
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
            # A crash can leave a running journal after its controller has
            # died. Persist the fact that this exact container was stopped,
            # so subsequent explicit native repair may start it again without
            # losing the proof that the old execution cannot still be alive.
            stopped = self._require_owned(record)
            if (not stopped.running
                    and stopped.container_id == record["runtime"]["container_id"]):
                for txn in self._pending_package_transactions(env_id):
                    if txn.get("environment_binding") != binding_for(record):
                        continue
                    for step in txn.get("native", []):
                        if (step.get("state") in {"starting", "running", "interrupted"}
                                and step.get("cleanup") not in {"gone", "container-stopped"}):
                            step["cleanup"] = "container-stopped"
                    txn["container_stopped_at"] = self.clock()
                    self.store.save_transaction(txn)
        return self.inspect(env_id)

    def run(self, env_id: str, argv: list[str], *, workdir: str | None,
            env_keys: list[str]) -> int:
        if not argv or any(not isinstance(item, str) or item == "" for item in argv):
            raise UnifierError("argv must be a non-empty list of non-empty strings")
        if workdir is not None and (not workdir.startswith("/") or "\x00" in workdir):
            raise UnifierError("workdir must be an absolute path inside the environment")
        record = self.store.load(validate_env_id(env_id))
        self._require_host_compatibility(record)
        info = self._require_owned(record)
        if not info.running:
            raise UnifierError(f"environment {record['id']} is {info.status}; start it before run")
        env = self._allowlist(env_keys)
        runtime = record["runtime"]
        result = self.runtime.exec(
            runtime["container_name"], argv, project=runtime["project"], env_id=record["id"],
            workdir=workdir, env=env, capture=False, timeout=None)
        return result.returncode

    def remove(self, env_id: str) -> None:
        env_id = validate_env_id(env_id)
        with self.package_coordination_lock():
            with self.package_operation_lock(env_id, blocking=False):
                self._remove_locked(env_id)

    def _remove_locked(self, env_id: str) -> None:
        with self._lifecycle_lock(env_id):
            from unifier.project_sessions import assert_environment_not_live
            assert_environment_not_live(self.root, env_id, self)
            assert_no_active_views(self.root, env_id)
            record = self.store.load(env_id)
            runtime = record["runtime"]
            name = runtime["container_name"]
            info = self.runtime.inspect(name)
            if info is None:
                self.store.delete(record["id"])
                return
            if not self._matches(info, record):
                raise UnifierError(
                    f"refusing to remove {name}: it does not belong to environment {record['id']}")
            self.runtime.remove(name, project=runtime["project"], env_id=record["id"])
            self.store.delete(record["id"])

    def add_app(self, env_id: str, app_id: str, executable: str, argv: list[str], *,
                workdir: str | None = None, env_overlay: dict | None = None,
                as_host_user: bool = False, on_demand: bool = False,
                exit_policy: str = 'wait',
                package_origin: dict | None = None,
                desktop_required: bool = True,
                alias: str | None = None) -> dict:
        self._validate_app_exit_policy(exit_policy, on_demand)
        app_id = validate_app_id(app_id)
        if alias is not None:
            alias = validate_app_id(alias)
        if self._launch_exists(app_id):
            raise UnifierError(f"application {app_id} already exists")
        record = self.store.load(validate_env_id(env_id))
        # New application records pin the exact environment instance. A legacy
        # environment must be migrated explicitly before it can gain new apps.
        binding = binding_for(record)
        desktop = record.get("desktop")
        if package_origin and package_origin.get("target") != record["target"]:
            candidates = package_origin.get("candidates") or []
            if package_origin.get("target") is None and len(candidates) > 1:
                raise UnifierError(
                    "package format matches more than one target: " + ", ".join(candidates))
            raise UnifierError("package format does not match the application environment")
        if not desktop and desktop_required:
            raise UnifierError(
                f"environment {record['id']} has no desktop session; create it with --desktop")
        executable = self._absolute_executable(executable)
        if not argv or argv[0] != executable:
            raise UnifierError("argv must start with the executable")
        if any(not isinstance(item, str) or item == "" for item in argv):
            raise UnifierError("argv must be a non-empty list of non-empty strings")
        workdir = self._shared_workdir(record, workdir)
        env = dict((desktop or {}).get("env") or {})
        for key, value in (env_overlay or {}).items():
            if not ENV_KEY.fullmatch(key) or not isinstance(value, str) or value == "" or "\n" in value:
                raise UnifierError(f"invalid application environment value: {key}")
            env[key] = value
        if on_demand and desktop_required and MODE_KEY not in env:
            env.update(default_portal_profile(record, executable))
        configured_environment(record, env, on_demand=on_demand)
        user = None
        if as_host_user:
            if record["runtime"].get("user_namespace") != "keep-id":
                raise UnifierError("--as-host-user requires an environment created with --userns keep-id")
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
                runtime_dir = ((desktop or {}).get("env") or {}).get(
                    "XDG_RUNTIME_DIR", f"/run/user/{self.uid}")
                directories = [env["HOME"], runtime_dir]
                if any(Path(directory).is_relative_to(item["container"])
                       for directory in directories for item in record["shared_mounts"]):
                    raise UnifierError("application home/runtime directory cannot be a shared mount")
                env["XDG_RUNTIME_DIR"] = runtime_dir
                prepared = self._exec_record(
                    self.store.load(record["id"]),
                    ["install", "-d", "-m", "700", "-o", str(self.uid), "-g", str(self.gid),
                     *directories], timeout=20, native=False)
                if prepared.returncode != 0:
                    raise UnifierError(f"cannot prepare application home/runtime: {prepared.stderr[-1500:]}")
            check = self._exec_record(self.store.load(record["id"]), ["test", "-x", executable],
                                      timeout=20, native=False)
            if check.returncode != 0:
                raise UnifierError(f"{executable} is not executable in {record['id']}")
        finally:
            if not was_running:
                with contextlib.suppress(Exception):
                    self.stop(record["id"])
        record = self.store.load(record["id"])
        launch = {
            "schema_version": 2,
            "id": app_id,
            "user_alias": alias or app_id,
            "app_instance_uuid": str(uuid.uuid4()),
            "environment_id": record["id"],
            "environment_uuid": binding["environment_uuid"],
            "generation": binding["generation"],
            "container_id": binding["container_id"],
            "environment_binding": binding,
            "executable": executable,
            "argv": list(argv),
            "workdir": workdir,
            "env": env,
            "user": user,
            "on_demand": on_demand,
            "package_origin": package_origin,
            "mounts": list((desktop or {}).get("mounts") or []),
            "export": None,
            "created_at": self.clock(),
            "updated_at": self.clock(),
        }
        if exit_policy != 'wait':
            launch['exit_policy'] = exit_policy
        self.store.save_launch(launch)
        return launch

    def show_app(self, app_id: str) -> dict:
        return self.store.load_launch(validate_app_id(app_id))

    def app_by_alias(self, env_id: str, alias: str) -> dict:
        """Resolve the user-facing alias inside one environment.

        New launches keep a namespaced internal id and a per-target
        ``user_alias``.  Legacy launches have no alias field, so their old id
        remains the accepted alias.  Two matches are ambiguous and refused.
        """
        env_id = validate_env_id(env_id)
        alias = validate_app_id(alias)
        matches = []
        for launch in self.store.launches_for(env_id):
            if launch.get("user_alias") == alias:
                matches.append(launch)
            elif not launch.get("user_alias") and launch.get("id") == alias:
                matches.append(launch)
        if not matches:
            raise UnifierError(f"application {alias} does not exist in {env_id}")
        if len(matches) > 1:
            raise UnifierError(
                f"application alias {alias} is ambiguous in {env_id}; "
                "remove the duplicate launch record explicitly")
        return matches[0]

    def open_app(self, app_id: str, extra: list[str] | None = None, *,
                 expected_app_instance_uuid: str | None = None,
                 expected_execution_fingerprint: str | None = None,
                 integration_uuid: str | None = None,
                 project_uuid: str | None = None) -> ExecResult:
        """Open one recorded application.

        On-demand applications take the lease in a supervised worker instead of
        this short-lived CLI process, so killing the launcher cannot lose
        automatic stop ownership; manual (non on-demand) apps stay unmanaged.
        A desktop launcher passes the journaled integration and exact app
        instance pins; the detached worker repeats the same checks itself.
        """
        launch = self.show_app(app_id)
        if integration_uuid is not None:
            self.validate_integration_launch(
                integration_uuid, app_id=app_id, project_uuid=project_uuid,
                app_instance_uuid=expected_app_instance_uuid,
                execution_fingerprint=expected_execution_fingerprint)
        if launch.get('on_demand'):
            from unifier.on_demand import supervised_open
            pins = {}
            if expected_app_instance_uuid is not None:
                pins["expected_app_instance_uuid"] = expected_app_instance_uuid
            if expected_execution_fingerprint is not None:
                pins["expected_execution_fingerprint"] = expected_execution_fingerprint
            if integration_uuid is not None:
                pins["integration_uuid"] = integration_uuid
            if project_uuid is not None:
                pins["project_uuid"] = project_uuid
            return supervised_open(self, app_id, extra or [], **pins)
        return self._open_app_unmanaged(
            app_id, extra,
            expected_app_instance_uuid=expected_app_instance_uuid,
            expected_execution_fingerprint=expected_execution_fingerprint)

    def _open_app_unmanaged(self, app_id: str, extra: list[str] | None = None, *,
                            expected_app_instance_uuid: str | None = None,
                            expected_execution_fingerprint: str | None = None) -> ExecResult:
        launch = self.show_app(app_id)
        argv = list(launch["argv"])
        for item in extra or []:
            if item == "":
                continue
            argv.append(item)
        return self.exec_app(
            app_id, argv, capture=False, timeout=None,
            expected_app_instance_uuid=expected_app_instance_uuid,
            expected_execution_fingerprint=expected_execution_fingerprint)

    def validate_app_binding(self, app_id: str) -> dict:
        """Read-only preflight used before a supervisor is spawned."""
        launch = self.show_app(app_id)
        self._require_app_execution(launch)
        self._validate_launch_binding(launch)
        return launch

    def set_app_file_chooser(self, app_id: str, mode: str) -> dict:
        with management_lock(self.root):
            launch = self.show_app(app_id)
            record = self.store.load(launch["environment_id"])
            self._validate_launch_binding(launch, record=record)
            env = dict(launch["env"])
            env[MODE_KEY] = mode
            configured_environment(record, env, on_demand=launch.get("on_demand"))
            if mode == "native":
                env.pop(MODE_KEY)
            if env != launch["env"]:
                launch["env"] = env
                self.store.save_launch(launch)
            return launch

    def set_app_input_method(self, app_id: str, mode: str) -> dict:
        with management_lock(self.root):
            launch = self.show_app(app_id)
            record = self.store.load(launch["environment_id"])
            self._validate_launch_binding(launch, record=record)
            validate_input_method(record, launch["env"], mode)
            # This preference selects a toolkit frontend on an existing X11
            # connection. It grants no new mount or D-Bus access, and does
            # not rewrite the launch environment pinned by desktop handlers.
            if mode != launch.get("input_method", "auto"):
                if mode == "auto":
                    launch.pop("input_method", None)
                else:
                    launch["input_method"] = mode
                self.store.save_launch(launch)
            return launch

    def validate_integration_launch(self, integration_uuid: str, *, app_id=None,
                                    project_uuid=None, app_instance_uuid=None,
                                    execution_fingerprint=None) -> dict:
        """Validate the journaled desktop integration that launched this argv.

        The journal's stored desktop path is authoritative for an explicitly
        installed entry; it need not live under this process's default
        HostDesktop directories.
        """
        from unifier.desktop_launch import integration_collection, validate_launcher
        from unifier.projects import ProjectRegistry

        return validate_launcher(
            integration_collection(self.root), ProjectRegistry(self.root).show, self,
            integration_uuid, project_uuid=project_uuid, app_id=app_id,
            app_instance_uuid=app_instance_uuid,
            execution_fingerprint=execution_fingerprint)

    @staticmethod
    def _validate_app_exit_policy(policy, on_demand):
        if policy not in ('wait', 'stop-children'):
            raise UnifierError('unknown application exit policy')
        if policy != 'wait' and not on_demand:
            raise UnifierError('stop-children exit policy requires an on-demand application')

    def _require_app_execution(self, launch: dict, *, app_instance_uuid=None,
                               execution_fingerprint=None) -> None:
        """Recheck the authorized app instance/config at a mutation boundary."""
        self._validate_app_exit_policy(launch.get('exit_policy', 'wait'),
                                       launch.get('on_demand'))
        if app_instance_uuid is not None:
            actual = launch.get("app_instance_uuid")
            if not isinstance(actual, str) or actual != app_instance_uuid:
                raise UnifierError(
                    "application instance changed; re-register the handler explicitly")
        if execution_fingerprint is not None:
            actual = app_execution_fingerprint(launch)
            if not isinstance(execution_fingerprint, str) or actual != execution_fingerprint:
                raise UnifierError(
                    "application execution configuration changed; "
                    "re-register the handler explicitly")

    def _require_record_binding(self, record: dict, binding: dict) -> None:
        """Compare the persisted environment instance with an application pin."""
        validate_binding(binding)
        if record.get("id") != binding["environment_id"]:
            raise UnifierError("application binding names a different environment")
        identity(record, required=True)
        schema_version(record)
        if record.get("environment_uuid") != binding["environment_uuid"]:
            raise UnifierError(
                "application binding is stale: the environment UUID was replaced; "
                "re-register the application explicitly")
        if record.get("generation") != binding["generation"]:
            raise UnifierError(
                "application binding is stale: the environment generation changed; "
                "re-register the application explicitly")
        runtime = record.get("runtime") or {}
        if runtime.get("container_id") != binding["container_id"]:
            raise UnifierError(
                "application binding is stale: the environment container instance changed; "
                "re-register the application explicitly")

    def _validate_launch_binding(self, launch: dict, *, record: dict | None = None) -> dict:
        binding = launch.get("environment_binding")
        if binding is None:
            raise UnifierError(
                f"application {launch.get('id')} has a legacy launch record without a "
                "versioned environment binding; re-register it explicitly with app remove/add")
        validate_binding(binding)
        environment_id = launch.get("environment_id")
        if environment_id != binding["environment_id"]:
            raise UnifierError("launch record and environment binding disagree")
        for key in ("environment_uuid", "generation", "container_id"):
            if key in launch and launch[key] != binding[key]:
                raise UnifierError(
                    f"launch record {key} disagrees with its environment binding; "
                    "re-register the application explicitly")
        if record is None:
            record = self.store.load(validate_env_id(environment_id))
        self._require_record_binding(record, binding)
        self._require_host_compatibility(record)
        runtime = record["runtime"]
        info = self.runtime.inspect(runtime["container_name"])
        if info is None:
            raise UnifierError(
                f"application {launch.get('id')} is bound to a container that no longer "
                "exists; re-register the application explicitly")
        if not self._matches(info, record) or info.container_id != binding["container_id"]:
            raise UnifierError(
                "application binding is stale: the current container does not match "
                "the recorded environment instance; re-register the application explicitly")
        return binding

    def exec_app(self, app_id: str, argv: list[str], *, capture: bool = False,
                 timeout: int | None = None, workdir: str | None = None,
                 expected_app_instance_uuid: str | None = None,
                 expected_execution_fingerprint: str | None = None) -> ExecResult:
        record, launch, effective_workdir, env = self._prepare_app_execution(
            app_id, argv, workdir=workdir,
            expected_app_instance_uuid=expected_app_instance_uuid,
            expected_execution_fingerprint=expected_execution_fingerprint)
        runtime = record['runtime']
        user_options = {'user': launch['user']} if launch.get('user') else {}
        argv = launch_argv(record, launch, argv, environment=env)
        return self.runtime.exec(
            runtime['container_name'], argv, project=runtime['project'], env_id=record['id'],
            workdir=effective_workdir, env=env, capture=capture,
            timeout=timeout, **user_options)

    def start_app_execution(self, app_id, argv, *, token, workdir=None,
                            expected_app_instance_uuid=None,
                            expected_execution_fingerprint=None):
        try:
            if not callable(getattr(self.runtime, 'start_application_exec', None)):
                raise UnifierError('runtime cannot supervise application descendants')
            record, launch, effective_workdir, env = self._prepare_app_execution(
                app_id, argv, workdir=workdir,
                expected_app_instance_uuid=expected_app_instance_uuid,
                expected_execution_fingerprint=expected_execution_fingerprint)
            argv = launch_argv(record, launch, argv, environment=env)
        except (UnifierError, OSError, ValueError) as exc:
            # This path has not submitted a guest application to the runtime.
            exc.execution_started = False
            raise
        runtime = record['runtime']
        policy = launch.get('exit_policy', 'wait')
        options = {'exit_policy': policy} if policy != 'wait' else {}
        return self.runtime.start_application_exec(
            runtime['container_id'], argv, token=token, project=runtime['project'],
            env_id=record['id'], workdir=effective_workdir, env=env,
            user=launch.get('user'), interactive=False, tty=False, **options)

    def _prepare_app_execution(self, app_id, argv, *, workdir=None,
                               expected_app_instance_uuid=None,
                               expected_execution_fingerprint=None):
        if not argv or any(not isinstance(item, str) or item == "" for item in argv):
            raise UnifierError("argv must be a non-empty list of non-empty strings")
        launch = self.show_app(app_id)
        self._require_app_execution(
            launch, app_instance_uuid=expected_app_instance_uuid,
            execution_fingerprint=expected_execution_fingerprint)
        binding = self._validate_launch_binding(launch)
        record = self.store.load(launch["environment_id"])
        preflight_picker(record, launch)
        workdir_override = None
        if workdir is not None:
            workdir_override = self._shared_workdir(record, workdir)
        # The lifecycle lock rechecks the binding and app instance before start.
        self.start(
            record["id"], expected_binding=binding,
            expected_app={
                "app_id": app_id,
                "app_instance_uuid": expected_app_instance_uuid,
                "execution_fingerprint": expected_execution_fingerprint,
            })
        record = self.store.load(record["id"])
        self._require_record_binding(record, binding)
        runtime = record["runtime"]
        info = self._require_owned(record)
        if info.container_id != binding["container_id"] or not info.running:
            raise UnifierError(
                "application binding changed before execution; refusing to run")
        # Re-read the launch at the exec boundary so a concurrent change to
        # env/user/workdir cannot use stale authorization.
        launch = self.show_app(app_id)
        self._require_app_execution(
            launch, app_instance_uuid=expected_app_instance_uuid,
            execution_fingerprint=expected_execution_fingerprint)
        preflight_picker(record, launch)
        effective_workdir = workdir_override if workdir is not None else launch.get("workdir")
        bridge = (record.get("desktop") or {}).get("portal_proxy") or {}
        options = {}
        if bridge.get("managed_mount") is True and launch["env"].get(MODE_KEY) in PORTAL_MODES:
            try:
                mount = self._resolve_managed_portal_mount(record)
                verify_active_portal_mount(info.pid, mount)
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot verify the active Portal bridge for {record['id']}: {exc}") from exc
            options["verified_portal_mount"] = mount.path
        env = launch_environment(record, launch, getattr(info, "mounts", ()), self.root, **options)
        apply_input_method(record, env, info.mounts, argv, mode=launch.get("input_method", "auto"))
        return record, launch, effective_workdir, env

    def export_app(self, app_id: str, *, mime_type: str = "application/x-multilinux-note",
                   extension: str = "mlnote", menu_only: bool = False,
                   display_name: str | None = None, icon: str | None = None,
                   wm_class: str | None = None, desktop_alias: str | None = None,
                   terminal: bool = False, extra_fields: dict | None = None) -> dict:
        if menu_only:
            return self._export_menu_app(
                app_id, display_name, icon, wm_class, desktop_alias,
                terminal=terminal, extra_fields=extra_fields)
        if not MIME_TYPE.fullmatch(mime_type):
            raise UnifierError(
                "file association type must look like application/x-multilinux-note")
        launch = self.show_app(app_id)
        self._validate_launch_binding(launch)
        if launch.get("export"):
            raise UnifierError(f"application {launch['id']} is already exported")
        xml = render_mime_xml(mime_type, extension)
        record = self.store.load(launch["environment_id"])
        project = record["runtime"]["project"]
        name = desktop_id(self.root, launch["id"])
        desktop_name = name + ".desktop"
        launcher = self._desktop_launcher(launch["id"])
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

    def _desktop_launcher(self, app_id):
        return ['/usr/bin/env', f'{ROOT_ENVIRONMENT}={self.root}', sys.executable,
                str(code_root() / 'tools/unifier.py'), 'app', 'open', app_id, '--']

    def _export_menu_app(self, app_id, display_name, icon, wm_class, desktop_alias,
                         terminal=False, extra_fields=None):
        launch = self.show_app(app_id)
        exported = launch.get('export')
        if exported:
            stale = (exported.get('mode') == 'menu'
                     and not Path(exported.get('desktop_path') or '').is_file())
            if not stale:
                raise UnifierError(f"application {app_id} is already exported")
            # The recorded menu entry was removed outside Unifier; re-register it.
            launch['export'] = None
        name = validate_app_id(desktop_alias) if desktop_alias else desktop_id(self.root, app_id)
        path = self.host_desktop.applications / (name + '.desktop')
        if path.exists():
            raise UnifierError(f'refusing to replace an existing desktop entry: {path}')
        record = self.store.load(launch['environment_id'])
        text = render_desktop_entry(
            launcher=self._desktop_launcher(app_id),
            app_id=app_id, environment_id=record['id'], project=record['runtime']['project'],
            mime_type=None, name=display_name or app_id, icon=icon, wm_class=wm_class,
            terminal=bool(terminal), extra_fields=extra_fields)
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
        with contextlib.suppress(UnifierError):
            self.host_desktop.refresh(self.cache_runner)
        launch["export"] = None
        launch["updated_at"] = self.clock()
        self.store.save_launch(launch)
        return launch

    def remove_app(self, app_id: str) -> None:
        if self._launch_exists(validate_app_id(app_id)):
            self.unexport_app(app_id)
        self.store.delete_launch(app_id)

    def _capture_discovery(self, env_id: str, argv: list[str], *,
                           timeout: int = 60) -> ExecResult:
        """Run one discovery query and release only a runtime started here."""
        was_running = self._ensure_running(env_id)
        try:
            return self._exec_record(
                self.store.load(env_id), argv, timeout=timeout, native=False)
        finally:
            if not was_running:
                with contextlib.suppress(Exception):
                    self.stop(env_id)

    def list_package_files(self, env_id: str, package: str) -> dict:
        """List files owned by one installed package with fixed native argv.

        No shell is involved and the package name is validated by the same
        rules as the native transaction.  This is used only to suggest
        application entries; it is not an authority to run the package.
        """
        name = validate_package_name(package)
        record = self.store.load(validate_env_id(env_id))
        argv = {
            "debian13": ["dpkg-query", "-L", name],
            "fedora44": ["rpm", "-ql", name],
            "arch": ["pacman", "-Ql", name],
        }.get(record["target"])
        if argv is None:
            raise UnifierError(f"no package-file query for target {record['target']}")
        result = self._capture_discovery(record["id"], argv)
        if result.returncode != 0:
            return {
                "package": name, "present": False, "files": [],
                "error": package_recovery.output_tail((result.stderr or result.stdout or "").strip(), 1500),
            }
        return {"package": name, "present": True,
                "files": parse_package_files(name, result.stdout or ""), "error": None}

    def local_package_name(self, env_id: str, host_file: str) -> str:
        """Read package identity with the selected guest's native reader."""
        env_id = validate_env_id(env_id)
        record = self.store.load(env_id)
        self._require_host_compatibility(record)
        local = local_package_path(record["target"], host_file)
        with self.package_operation_lock(env_id):
            was_running = self._ensure_running(env_id)
            try:
                record = self.store.load(env_id)
                incoming = self._stage_local(record, local)
                argv = {
                    "debian13": ["dpkg-deb", "-f", incoming, "Package"],
                    "fedora44": ["rpm", "-qp", "--qf", "%{NAME}\\n", incoming],
                    "arch": ["pacman", "-Qp", incoming],
                }[record["target"]]
                result = self._exec_record(record, argv, timeout=60, native=True)
                if result.returncode:
                    raise UnifierError(
                        "native package metadata query failed: "
                        + (result.stderr or result.stdout or "unknown error")[-1500:])
                words = result.stdout.strip().split()
                if not words:
                    raise UnifierError("native package metadata has no package name")
                return validate_package_name(words[0])
            finally:
                if not was_running:
                    self.stop(env_id, package_lock_held=True)

    def resolve_executable(self, env_id: str, executable: str) -> str:
        """Resolve an explicit guest command to an absolute path.

        A bare name is resolved by the guest shell's ``command -v``; no other
        user-controlled shell text is accepted.  The result must be absolute.
        """
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise UnifierError("executable must be a non-empty string")
        if executable.startswith("/"):
            return self._absolute_executable(executable)
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", executable):
            raise UnifierError(
                "executable must be an absolute path or a plain command name")
        record = self.store.load(validate_env_id(env_id))
        result = self._capture_discovery(
            record["id"],
            ["/bin/sh", "-c", 'command -v -- "$1"', "unifier-which", executable])
        resolved = (result.stdout or "").strip()
        if result.returncode != 0 or not resolved.startswith("/"):
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise UnifierError(
                f"command {executable!r} was not found in {record['id']}: {detail}")
        return resolved

    def discover_app_entries(self, env_id: str, packages: list[str]) -> list[dict]:
        """Suggest entries from installed package files and .desktop files.

        Candidates are data, never executed during discovery.  A desktop
        candidate keeps the complete literal Exec argv (minus file/URL/icon
        field codes), so two components sharing one executable (for example
        Writer and Sheets both using the same binary with different switches)
        remain distinct identities.  Unsupported shell/wrapper entries are
        omitted; the caller must then ask for an explicit --exec instead of
        receiving a wrong launch.
        """
        candidates = {}
        for package in packages:
            if not package:
                continue
            listing = self.list_package_files(env_id, package)
            for path in listing.get("files") or []:
                kind = package_entry_kind(path)
                if kind == "executable":
                    argv, source = [path], f"package-file:{package}"
                    desktop_id_value, menu = None, None
                elif kind == "desktop":
                    record = self.store.load(validate_env_id(env_id))
                    result = self._capture_discovery(record["id"], ["cat", path], timeout=30)
                    if result.returncode != 0:
                        continue
                    argv = desktop_exec_argv(result.stdout or "")
                    if not argv:
                        continue
                    if not argv[0].startswith("/"):
                        try:
                            argv[0] = self.resolve_executable(env_id, argv[0])
                        except UnifierError:
                            continue
                    source, desktop_id_value = f"desktop:{path}", Path(path).stem
                    menu = desktop_menu_fields(result.stdout or "")
                else:
                    continue
                # First occurrence wins the launch identity. A later desktop
                # file with the same argv still supplies the menu name.
                key = tuple(argv)
                current = candidates.get(key)
                if current is None:
                    candidates[key] = {
                        "argv": list(argv), "executable": argv[0], "source": source,
                        "path": path, "desktop_id": desktop_id_value,
                        "menu": menu if kind == "desktop" else None,
                    }
                elif kind == "desktop" and not current.get("menu"):
                    current["menu"] = menu
                    current["menu_desktop_id"] = desktop_id_value
        return list(candidates.values())

    @contextlib.contextmanager
    def application_registration_session(self, env_id: str):
        """Pin package state and lease ownership during registration of an installed app."""
        from unifier.on_demand import LaunchGroup

        env_id = validate_env_id(env_id)
        group = LaunchGroup(self, env_id)
        with (self.package_coordination_lock(), group.locked(),
              self.package_operation_lock(env_id, blocking=False)):
            self._require_recovered_packages(env_id)
            record = self.store.load(env_id)
            state = group.read()
            was_running = self._require_owned(record).running
            if not was_running:
                if state["leases"] or state["bridge"] is not None:
                    raise UnifierError(f"cannot start {env_id} for registration while it has leases or bridge state")
                self.start(env_id, expected_binding=binding_for(record))
            try:
                yield
            finally:
                if not was_running:
                    self.stop(env_id, package_lock_held=True)

    def query_package(self, env_id: str, name: str, *, require_installed: bool = False) -> dict:
        name = validate_package_name(name)
        record = self.store.load(validate_env_id(env_id))
        driver = driver_for(record)
        was_running = self._ensure_running(record["id"])
        try:
            result = self._exec_record(
                self.store.load(record["id"]), driver.query(name),
                timeout=30, native=True)
            present, detail = driver.query_result(
                result.returncode, result.stdout, result.stderr,
                require_installed=require_installed)
            return {
                "environment_id": record["id"],
                "package": name,
                "present": present,
                "exit_code": result.returncode,
                "detail": detail[-1500:],
            }
        finally:
            if not was_running:
                with contextlib.suppress(Exception):
                    self.stop(record["id"])

    def install_packages(self, env_id: str, packages: list[str], *, use_argv=None,
                         interactive=False, stream=False, repair_dependencies=False,
                         application_runtime=False) -> dict:
        return self._package_transaction(env_id, "install", packages, use_argv=use_argv,
                                         interactive=interactive, stream=stream,
                                         repair_dependencies=repair_dependencies,
                                         application_runtime=application_runtime)

    def update_packages(self, env_id: str, *, use_argv=None, interactive=False, stream=False) -> dict:
        return self._package_transaction(env_id, "update", [], use_argv=use_argv,
                                         interactive=interactive, stream=stream)

    def remove_packages(self, env_id: str, packages: list[str], *, use_argv=None,
                        interactive=False, stream=False) -> dict:
        return self._package_transaction(env_id, "remove", packages, use_argv=use_argv,
                                         interactive=interactive, stream=stream)

    def install_local(self, env_id: str, host_file: str, *, package_name: str | None = None,
                      use_argv=None, interactive=False, stream=False,
                      repair_dependencies=False, application_runtime=False) -> dict:
        if repair_dependencies and package_name is None:
            package_name = self.local_package_name(env_id, host_file)
        names = [package_name] if package_name else []
        return self._package_transaction(
            env_id, "install-local", names, use_argv=use_argv, local_file=host_file,
            interactive=interactive, stream=stream, repair_dependencies=repair_dependencies,
            application_runtime=application_runtime)

    def repair_dependencies(self, env_id: str, packages: list[str], *, libraries=(),
                            interactive=False, stream=False, diagnostics_path=None) -> dict:
        from unifier.dependencies import validate_library
        libraries = [validate_library(name) for name in libraries]
        dependency_input = None
        if diagnostics_path is not None:
            from unifier.dependency_diagnostics import read_diagnostics
            dependency_input = read_diagnostics(diagnostics_path)
            blocked = sorted({issue["kind"] for issue in dependency_input["issues"]
                              if issue["kind"] != "missing-library"})
            if blocked:
                raise UnifierError("diagnostic log contains failures outside automatic library repair: "
                                   + ", ".join(blocked) + "; use --diagnose-only for classification")
            if not dependency_input["libraries"]:
                raise UnifierError("diagnostic log contains no exact missing-library error")
            libraries = sorted(set(libraries) | set(dependency_input["libraries"]))
        if not packages and not libraries:
            raise UnifierError("dependency repair requires installed packages or --library")
        return self._package_transaction(env_id, "repair-dependencies", packages,
                                         interactive=interactive, stream=stream,
                                         repair_dependencies=True, libraries=libraries,
                                         dependency_input=dependency_input,
                                         application_runtime=bool(packages))

    def transactions_for(self, env_id: str) -> list[dict]:
        return self.store.transactions_for(validate_env_id(env_id))

    def show_transaction(self, txn_id: str) -> dict:
        if not re.fullmatch(r"txn[0-9a-f]{12}", txn_id):
            raise UnifierError("transaction id must look like txn followed by 12 hex digits")
        return self.store.load_transaction(txn_id)

    def _pending_package_transactions(self, env_id: str) -> list[dict]:
        record = self.store.load(env_id)
        return [txn for txn in self.store.transactions_for(env_id)
                if (txn.get("phase") in {"running", "needs-recovery"}
                    or txn.get("recovery_required"))
                and not package_recovery.maintenance_reconciled(record, txn)]

    def _require_recovered_packages(self, env_id: str) -> None:
        if (self.store.load(env_id).get("package_recovery_required")
                or self._pending_package_transactions(env_id)):
            raise UnifierError(
                f"package state for {env_id} needs recovery; run 'unifier pkg recover {env_id}' "
                "before another package change")

    def _save_package_recovery_state(self, binding: dict, *, status: str, required: bool) -> None:
        env_id = binding["environment_id"]
        with self._lifecycle_lock(env_id):
            latest = self.store.load(env_id)
            self._require_record_binding(latest, binding)
            latest.update(package_inventory_status=status, package_recovery_required=required)
            self.store.save(latest, None)

    def _audit_package_database(self, record: dict, txn: dict) -> None:
        package_recovery.audit_package_database(self, record, txn)

    def _native_package_journal(self, record, action, audit):
        return package_recovery.native_package_journal(self, record, action, audit)

    def recover_packages(self, env_id: str, *, maintenance_receipt: dict | None = None) -> dict:
        """Reconcile a stopped execution, without silently re-running scripts."""
        env_id = validate_env_id(env_id)
        with self.package_coordination_lock():
            return self._recover_packages_locked(env_id, maintenance_receipt)

    def recover_inventory_storage(self, env_id: str) -> dict:
        env_id = validate_env_id(env_id)
        with self.package_coordination_lock():
            with self.package_operation_lock(env_id, blocking=False):
                with self._lifecycle_lock(env_id):
                    record = self.store.load(env_id)
                    reader = getattr(self.runtime, 'recover_inventory_storage', None)
                    if not callable(reader):
                        raise UnifierError('runtime has no inventory storage recovery adapter')
                    binding = binding_for(record)
                    result = reader(record)
                    self._require_record_binding(self.store.load(env_id), binding)
                    return {'environment_id': env_id, **result}

    def _recover_packages_locked(self, env_id: str, maintenance_receipt=None) -> dict:
        return package_recovery.recover_locked(self, env_id, maintenance_receipt)

    def _package_transaction(self, env_id: str, operation: str, packages: list[str], *,
                             use_argv=None, local_file: str | None = None,
                             interactive=False, stream=False, repair_dependencies=False,
                             libraries=(), dependency_input=None, application_runtime=False,
                             desktop_clients=()) -> dict:
        """Serialize the complete isolation window, then the native DB writer.

        Order: management -> environment package -> lifecycle. Neither the
        package path nor lifecycle calls acquire a lease lock. Lease holders
        only try the environment package lock without waiting when stopping.
        Internal stop calls must not reacquire either package lock.
        """
        env_id = validate_env_id(env_id)
        self.store.load(env_id)
        with self.package_coordination_lock():
            with self.package_operation_lock(env_id):
                self._require_recovered_packages(env_id)
                return self._package_transaction_locked(
                    env_id, operation, packages, use_argv=use_argv,
                    local_file=local_file, interactive=interactive, stream=stream,
                    repair_dependencies=repair_dependencies, libraries=libraries,
                    dependency_input=dependency_input, application_runtime=application_runtime,
                    desktop_clients=desktop_clients)

    def _package_transaction_locked(self, env_id: str, operation: str,
                                    packages: list[str], *,
                                    use_argv=None,
                                    local_file: str | None = None, interactive=False,
                                    stream=False, repair_dependencies=False, libraries=(),
                                    dependency_input=None, application_runtime=False,
                                    desktop_clients=()) -> dict:
        return package_transaction.run_locked(
            self, env_id, operation, packages, use_argv=use_argv, local_file=local_file,
            interactive=interactive, stream=stream, repair_dependencies=repair_dependencies,
            libraries=libraries, dependency_input=dependency_input,
            application_runtime=application_runtime, desktop_clients=desktop_clients)

    def _finalize_package_transaction(self, record, txn, inventory_text,
                                      was_running, other_before, host_before):
        package_transaction.finalize(
            self, record, txn, inventory_text, was_running, other_before, host_before)

    def _application_python_ready(self, record: dict) -> bool:
        result = self._exec_record(
            record, ["/bin/sh", "-c", APPLICATION_PYTHON_CHECK], timeout=10,
            native=False, pin_package=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise UnifierError(
                f"guest Python 3 check failed (exit {result.returncode}): {detail}")
        return (result.stdout or "").strip() != APPLICATION_PYTHON_MISSING

    def _desktop_fonts_ready(self, record: dict) -> bool:
        check = (
            "command -v fc-match >/dev/null 2>&1 || exit 1\n"
            "font=$(fc-match --format '%{file}' sans-serif:lang=en) || exit 2\n"
            'test -n "$font" && test -r "$font"'
        )
        result = self._exec_record(
            record, ["/bin/sh", "-c", check], timeout=10, native=False, pin_package=True)
        if result.returncode not in (0, 1):
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise UnifierError(f"guest font check failed (exit {result.returncode}): {detail}")
        return result.returncode == 0

    def _stage_local(self, record: dict, local_path: Path) -> str:
        container_path = f"{INCOMING_DIR}/{local_path.name}"
        self._exec_record(record, ["mkdir", "-p", INCOMING_DIR], timeout=30,
                          native=False, pin_package=True)
        runtime = record["runtime"]
        self.runtime.copy_in(
            self._package_container_ref(record), str(local_path), container_path,
            project=runtime["project"], env_id=record["id"])
        return container_path

    def _run_native(self, record: dict, txn: dict, steps: list[tuple[list[str], int]]) -> None:
        package_transaction.run_native(self, record, txn, steps)

    def _verify(self, record: dict, operation: str, names: list[str], use_argv: list[str],
                inventory_text: str, txn: dict) -> dict:
        present = {name: inventory_has(inventory_text, name) for name in names}
        problems = []
        if txn["failure_phase"] is not None:
            return {
                "ok": False,
                "packages": present,
                "command": None,
                "command_exit_code": None,
                "stdout_preview": None,
                "stdout_sha256": None,
                "problems": ["native command failed" if txn["failure_phase"] == "native-command"
                             else f"{txn['failure_phase']} failed"],
            }
        if operation == "remove":
            driver = driver_for(record)
            if driver.name == "apt":
                # Both running and stopped inventory preserve dpkg records for
                # deinstalled packages whose configuration files still exist.
                for name, found in present.items():
                    if not found:
                        continue
                    result = self._exec_record(record, driver.query(name), timeout=30,
                                               native=True, pin_package=True)
                    if result.returncode == 0:
                        present[name], _ = driver.query_result(
                            result.returncode, result.stdout, result.stderr)
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
        if (package_transaction.syu_succeeded(txn["native"])
                and driver_for(record["target"]).tracks_package_set
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
            host_after = unavailable_host(exc)
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
        if (not host_before or (host_before.get('entries') or {}).get('available') is False
                or (host_after.get('entries') or {}).get('available') is False):
            txn['host_entries'] = {'available': False, 'added': [], 'removed': []}
        else:
            txn["host_entries"] = entry_changes(list(before_names), list(after_names))
        txn['isolation_observation'] = summarize_observation(txn)

    def _other_fingerprints(self, env_id: str) -> dict:
        found = {}
        for other in self.store.all():
            if other["id"] == env_id:
                continue
            evidence = {}
            probed, error = self._probe_inventory(other, evidence=evidence)
            found[other["id"]] = {
                "stored": other["package_inventory_sha256"],
                "probed": probed,
                "error": error,
                "binding": binding_for(other),
                "observation": evidence,
            }
        return found

    def _probe_inventory(self, record: dict, *, evidence=None) -> tuple[str | None, str | None]:
        """Observe a peer without starting it or executing anything inside it.

        Runtime and fake readers share one explicit observation contract.
        The registered hash cannot substitute for a current database read.
        """
        try:
            # A peer start/stop from another product entry must not race a
            # stopped-database read. Package callers already hold management
            # coordination; lifecycle paths never reacquire that lock.
            with self._lifecycle_lock(record['id']):
                expected = binding_for(record)
                self._require_record_binding(self.store.load(record['id']), expected)
                before = self._require_owned(record)
                reader = getattr(self.runtime, "read_inventory_readonly", None)
                if not callable(reader):
                    raise UnifierError("read-only peer inventory unavailable; package operation refused")
                value = reader(record)
                if not isinstance(value, InventoryObservation):
                    raise UnifierError("read-only peer inventory unavailable: reader violated the observation contract")
                if evidence is not None:
                    evidence.update(value.evidence())
                if value.binding != expected or value.target != record['target']:
                    raise UnifierError('read-only inventory observation binding changed')
                if value.status != 'supported' or value.inventory is None:
                    raise UnifierError(value.reason or 'read-only database observation unsupported')
                text = value.inventory
                if not isinstance(text, str) or text != normalize_inventory(text):
                    raise UnifierError('read-only inventory is not canonical text')
                self._require_record_binding(self.store.load(record['id']), expected)
                after = self._require_owned(record)
                if (after.container_id != before.container_id or after.running != before.running
                        or after.started_at != before.started_at or after.finished_at != before.finished_at):
                    raise UnifierError('peer container state changed during observation')
                return hashlib.sha256(text.encode()).hexdigest(), None
        except Exception as exc:
            return None, str(exc)[-500:]

    def _operation_lock_path(self, env_id: str) -> Path:
        return self.root / ".lab/state/operations" / f"{env_id}.package.lock"

    def package_coordination_lock(self, *, timeout: float = 30.0,
                                  cancel=None, blocking: bool = True):
        """Cross-process admission for package writes and membership changes.

        Waiting has no runtime side effects. This lock is deliberately separate
        from the per-environment lock used by lease cleanup: app start/stop
        never waits for a management-root observation window.
        """
        return management_lock(self.root, timeout=timeout, cancel=cancel,
                               blocking=blocking)

    def _open_operation_lock(self, env_id: str) -> int:
        path = self._operation_lock_path(env_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(path, os.O_CREAT | os.O_RDWR, 0o600)

    @contextlib.contextmanager
    def package_operation_lock(self, env_id: str, *, blocking: bool = True):
        """Lock one environment's native package transaction."""
        descriptor = self._open_operation_lock(env_id)
        try:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, flags)
            except BlockingIOError as exc:
                raise UnifierError(
                    f"a native package transaction is in progress for {env_id}") from exc
            yield
        finally:
            os.close(descriptor)

    def try_package_operation_lock(self, env_id: str) -> int | None:
        """Take the package lock without waiting; return its fd or None."""
        descriptor = self._open_operation_lock(env_id)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            os.close(descriptor)
            return None

    @staticmethod
    def release_package_operation_lock(descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            pass

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
                raise UnifierError(
                    f"environment {record['id']} is still stopping; refusing to race podman")
            time.sleep(LIFECYCLE_SETTLE_POLL_SECONDS)
            info = self._require_owned(record)
        return info

    def _ensure_running(self, env_id: str) -> bool:
        record = self.store.load(env_id)
        self._require_host_compatibility(record)
        info = self._require_owned(record)
        if info.running:
            return True
        self.start(env_id)
        return False

    def _package_container_ref(self, record: dict) -> str:
        """Validate the journal's instance and pin real Podman calls to its CID."""
        self._require_record_binding(self.store.load(record['id']), binding_for(record))
        info = self._require_owned(record)
        if info.container_id != record["runtime"]["container_id"]:
            raise UnifierError("package container identity changed; refusing to use a replacement")
        return info.container_id if isinstance(self.runtime, PodmanRuntime) else info.name

    def _exec_record(self, record: dict, argv: list[str], *, timeout: int | None, native: bool,
                     pin_package: bool = False):
        self._require_host_compatibility(record)
        runtime = record["runtime"]
        return self.runtime.exec(
            self._package_container_ref(record) if pin_package else runtime["container_name"],
            argv, project=runtime["project"], env_id=record["id"],
            workdir=None, env=driver_for(record).native_env() if native else {},
            capture=True, timeout=timeout)

    def _read_inventory_record(self, record: dict) -> str:
        spec = CreateSpec(
            name=self._package_container_ref(record),
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
        return stamp_new_identity({
            "schema_version": 2,
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
                {"host": host, "container": container,
                 "options": share_volume_options(spec.share_label_policy)}
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
                "share_label_policy": spec.share_label_policy,
                "selinux_label": spec.selinux_label,
                "full_init": False,
                "user_namespace": spec.user_namespace,
                "devices": list(spec.devices),
                **({"cuda": spec.cuda.identity()} if spec.cuda is not None else {}),
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
        })

    def _live(self, record: dict) -> dict:
        view = dict(record)
        pending = self._pending_package_transactions(record["id"])
        if pending:
            view["package_inventory_status"] = "unknown"
            view["package_recovery_required"] = True
            view["pending_package_transactions"] = [txn["id"] for txn in pending]
        view["environment_uuid"] = record.get("environment_uuid")
        view["generation"] = record.get("generation")
        view["runtime_bind_mounts"] = None
        view["share_label_policy"] = effective_share_label_policy(record)
        try:
            view["compatibility"] = self.compatibility_plan(record["target"])
        except UnifierError as exc:
            view["compatibility"] = {"allowed": False, "reasons": [str(exc)]}
        info = self.runtime.inspect(record["runtime"]["container_name"])
        if info is None:
            view["runtime_state"] = "missing"
            view["resources"] = None
        elif not self._matches(info, record):
            view["runtime_state"] = "foreign"
            view["resources"] = None
        else:
            view["runtime_state"] = "running" if info.running else "stopped"
            if info.container_id == record["runtime"].get("container_id"):
                view["runtime_bind_mounts"] = info.bind_mounts
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
            raise UnifierError("executable must be an absolute path inside the environment")
        return executable

    def _shared_workdir(self, record: dict, workdir: str | None) -> str | None:
        """Accept a new application workdir only inside a real shared directory.

        The lexical check rejects ``..`` outright. Then the real path of the
        workdir must still be under the real path of the selected shared root,
        so a symlink inside the share cannot register a host path outside it.
        This is a registration-time check; it cannot constrain later writes by
        an environment process that already has the mount.
        """
        if workdir is None:
            return None
        if not workdir.startswith("/") or "\n" in workdir or "\x00" in workdir:
            raise UnifierError("workdir must be an absolute path")
        parts = PurePosixPath(workdir).parts
        if ".." in parts:
            raise UnifierError("workdir must not contain '..' components")
        shares = [item["host"] for item in record.get("shared_mounts") or []]
        selected = None
        for host in shares:
            host_parts = PurePosixPath(host).parts
            if parts[:len(host_parts)] == host_parts:
                if selected is None or len(host_parts) > len(PurePosixPath(selected).parts):
                    selected = host
        if selected is None:
            raise UnifierError("workdir must stay inside a shared mount")
        try:
            real_workdir = Path(workdir).resolve(strict=True)
            real_share = Path(selected).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UnifierError(f"workdir is not a valid shared directory: {exc}") from exc
        if not real_workdir.is_dir():
            raise UnifierError("workdir must be an existing directory inside a shared mount")
        if not real_workdir.is_relative_to(real_share):
            raise UnifierError(
                "workdir resolves outside its shared mount; symlink escapes are refused")
        return workdir

    def _remove_our_desktop_file(self, path: Path, launch: dict) -> None:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        if f"X-Multilinux-App={launch['id']}" not in text.splitlines():
            raise UnifierError(f"refusing to remove {path}: it is not application {launch['id']}")
        path.unlink()

    def _validate_identity(self, target_name: str, target: dict, identity: dict) -> None:
        if identity["id"] != target["expected_id"]:
            raise UnifierError(
                f"{target_name} reported distribution {identity['id']!r}, "
                f"expected {target['expected_id']!r}")
        expected_version = target.get("expected_version_id")
        if expected_version is not None and identity["version_id"] != expected_version:
            raise UnifierError(
                f"{target_name} reported version {identity['version_id']!r}, "
                f"expected {expected_version!r}")
        if identity["machine"] != "x86_64":
            raise UnifierError(f"{target_name} reported architecture {identity['machine']!r}")
        if identity["kernel"] != self.host_kernel:
            raise UnifierError(
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
            raise UnifierError(f"{' '.join(argv)} failed in {spec.env_id}: {detail[-1500:]}")
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
                raise UnifierError(f"invalid environment variable name: {key}")
            if key not in os.environ:
                raise UnifierError(f"allowlisted variable is not set: {key}")
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
                raise UnifierError(f"shared path must be absolute: {raw}")
            resolved = path.resolve()
            if not resolved.is_dir():
                raise UnifierError(f"shared path is not a directory: {resolved}")
            container = resolved.as_posix()
            if ":" in container or "\n" in container or "\x00" in container:
                raise UnifierError(
                    f"shared path cannot contain ':' or a newline; podman --volume uses ':' as a separator: {container}")
            if _hides_system_tree(container):
                raise UnifierError(f"refusing to mount over a system directory: {container}")
            if resolved == root or root.is_relative_to(resolved):
                raise UnifierError(f"refusing to share a parent of the project: {resolved}")
            if resolved == state or resolved.is_relative_to(state) or state.is_relative_to(resolved):
                raise UnifierError(f"refusing to share laboratory state: {resolved}")
            if container in seen:
                raise UnifierError(f"duplicate shared path: {container}")
            seen.add(container)
            mounts.append((container, container))
        return mounts

    def _target(self, name: str) -> dict:
        try:
            return self.config["targets"][name]
        except KeyError as exc:
            raise UnifierError(f"unknown target {name}") from exc

    def _plan_share(self, raw: str) -> str:
        path = Path(raw)
        if not path.is_absolute():
            raise UnifierError(f"shared path must be absolute: {raw}")
        return path.resolve().as_posix()

    def _lock_digest(self, target_name: str) -> str:
        return self._locked_reference(target_name).rsplit("@", 1)[1]

    def _locked_reference(self, target_name: str) -> str:
        try:
            item = self.lock["targets"][target_name]
        except KeyError as exc:
            raise UnifierError(f"image lock has no entry for {target_name}") from exc
        digest = item.get("manifest_digest", "")
        reference = item.get("locked_reference", "")
        if not DIGEST.fullmatch(digest) or not reference.endswith("@" + digest):
            raise UnifierError(f"refusing to create {target_name} from an unlocked reference")
        return reference

    def _require_owned(self, record: dict) -> ContainerState:
        info = self.runtime.inspect(record["runtime"]["container_name"])
        if info is None:
            raise UnifierError(f"environment {record['id']} has no container")
        if not self._matches(info, record):
            raise UnifierError(
                f"refusing to operate on {record['runtime']['container_name']}: "
                f"it does not belong to environment {record['id']}")
        return info

    def _require_owned_record(self, name: str, project: str, env_id: str) -> ContainerState:
        info = self.runtime.inspect(name)
        if info is None or info.project != project or info.env_id != env_id or info.role != ROLE_ENVIRONMENT:
            raise UnifierError(f"created container {name} failed the ownership check")
        if not info.container_id:
            raise UnifierError(f"created container {name} has no id")
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
