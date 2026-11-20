"""Classify registered hardware selections and explicit probe evidence without I/O."""

from collections.abc import Mapping
import json
import os
import re
import stat

from unifier.camera_device import CAMERA_PATH
from unifier.cuda_device import GPU_UUID, PCI_BDF
from unifier.environment_contract import validate_env_id
from unifier.errors import UnifierError
from unifier.identity import binding_for, schema_version, validate_binding
from unifier.record_ids import validate_uuid
from unifier.render_node import FULL_CID, RENDER_NODE


SCHEMA_VERSION = 1
RESULTS = ("PASS", "FAIL", "PARTIAL", "NOT_RUN", "BLOCKED", "UNKNOWN", "INVALID")
CAPABILITY_STATES = ("available", "unavailable", "unsupported", "restricted", "unknown")
PRIMARY_DRM = re.compile(r"/dev/dri/card[0-9]+")
SHA256 = re.compile(r"[a-f0-9]{64}")
PROVENANCE_FIELDS = ("boot_id", "device_identity", "backend", "backend_version")
CPU = ("cpu", "host-cpu")
MAX_RECEIPT_BYTES = 64 * 1024

RECOVERY_HINTS = {
    "device-missing": "Check the registered node and inspect the selected PCI device.",
    "device-unreadable": "Check host read/write access for the registered device.",
    "device-changed": "Recheck the physical device before selecting a render node again.",
    "device-unobserved": "Verify the registered device on this host before using the receipt.",
    "driver-unavailable": "Check the driver and userspace packages required by the probe.",
    "permission-denied": "Check the device permissions and authorized mappings for this environment.",
    "toolchain-mismatch": "Use a probe built for the selected device and installed driver.",
    "budget-rejected": "Check the test guard receipt and run again when its admission conditions hold.",
    "evidence-provenance": "Run a guarded probe with the selected backend and verify its runtime version.",
    "provenance-changed": "Repeat the probe on the current boot with the current backend.",
    "software-fallback": "Inspect the selected driver before relying on hardware acceleration.",
    "environment-changed": "Use evidence collected from the current container binding.",
    "cleanup-unverified": "Verify that the probe and its resources were released.",
}


def read_receipt(path):
    """Read one explicit probe receipt as bounded data, never as a live observation."""
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_RECEIPT_BYTES:
            raise ValueError("expected a regular non-empty file of at most 64 KiB")
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            current = os.fstat(stream.fileno())
            if not os.path.samestat(before, current) or not stat.S_ISREG(current.st_mode):
                raise ValueError("probe receipt was replaced while opening it")
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
            if len(raw) > MAX_RECEIPT_BYTES:
                raise ValueError("probe receipt exceeds 64 KiB")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected one probe receipt object")
        return value
    except (OSError, ValueError, RecursionError) as exc:
        raise UnifierError(f"cannot read hardware evidence {path}: {exc}") from exc


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _binding(value):
    binding = validate_binding(value)
    validate_env_id(binding["environment_id"])
    if not _matches(FULL_CID, binding["container_id"]):
        raise UnifierError("hardware evidence requires a complete container ID")
    return {key: binding[key] for key in (
        "environment_id", "environment_uuid", "generation", "container_id")}


def _registered_binding(record, observed_binding):
    try:
        if not isinstance(record, dict):
            raise UnifierError("hardware report requires an environment record")
        validate_env_id(record.get("id"))
        version = schema_version(record)
        if (version == 1 and "environment_uuid" not in record
                and "generation" not in record):
            return None, "legacy"
        expected = _binding(binding_for(record))
        if record.get("writable_revision", expected["container_id"]) != expected["container_id"]:
            raise UnifierError("recorded container identifiers disagree")
    except UnifierError:
        return None, "invalid"
    if observed_binding is None:
        return expected, "registered"
    try:
        observed = _binding(observed_binding)
    except UnifierError:
        return expected, "unverified"
    return expected, "matched" if observed == expected else "changed"


def _label(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _device_identity(value):
    return (isinstance(value, dict) and 0 < len(value) <= 16
            and all(_label(key) and (_label(item) or type(item) is int)
                    for key, item in value.items()))


def _selection(kind, selector, origin, identity=None, *, valid=True):
    return {
        "kind": kind, "selector": selector, "origin": origin,
        "state": "invalid" if not valid else "configured" if identity else "legacy",
        "identity": dict(identity) if valid and identity else None,
    }


def _render_minor(path):
    if _matches(RENDER_NODE, path):
        minor = int(path.removeprefix("/dev/dri/renderD"))
        if minor >= 128:
            return minor
    return None


def _render_identity(identity, minor):
    if not _device_identity(identity) or identity.get("major") != 226 or identity.get("minor") != minor:
        return False
    device = identity.get("sysfs_device")
    return (isinstance(device, str) and device.startswith("/devices/")
            and all(part not in ("", ".", "..") for part in device[1:].split("/")))


def _selections(runtime):
    rows = []
    if "render_node" in runtime:
        path = runtime["render_node"]
        minor = _render_minor(path)
        identity = runtime.get("render_node_identity")
        valid = minor is not None and ("render_node_identity" not in runtime
                                       or _render_identity(identity, minor))
        rows.append(_selection("render-node", path if minor is not None else None, "managed",
                               identity, valid=valid))
    elif "render_node_identity" in runtime:
        rows.append(_selection("render-node", None, "managed", valid=False))
    if "camera" in runtime:
        camera = runtime["camera"]
        path = camera.get("path") if isinstance(camera, dict) else None
        selected = _matches(CAMERA_PATH, path)
        selection = camera.get("selection") if isinstance(camera, dict) else None
        valid = selected and _matches(SHA256, selection)
        rows.append(_selection("camera", path if selected else None, "managed",
                               {"selection": selection}, valid=valid))
    if "cuda" in runtime:
        cuda = runtime["cuda"]
        uuid = cuda.get("uuid") if isinstance(cuda, dict) else None
        selected = _matches(GPU_UUID, uuid)
        valid = (selected and _device_identity(cuda) and _matches(PCI_BDF, cuda.get("pci"))
                 and type(cuda.get("minor")) is int and 0 <= cuda["minor"] < 255
                 and _label(cuda.get("library")) and cuda["library"].startswith("/"))
        rows.append(_selection("cuda", uuid if selected else None, "creation", cuda, valid=valid))
    if "devices" in runtime:
        devices = runtime["devices"]
        if not isinstance(devices, list):
            rows.append(_selection("device", None, "creation", valid=False))
        else:
            for path in devices:
                if _render_minor(path) is not None:
                    rows.append(_selection("render-node", path, "creation"))
                elif _matches(PRIMARY_DRM, path):
                    rows.append(_selection("primary-drm", path, "creation"))
                elif _matches(CAMERA_PATH, path):
                    rows.append(_selection("camera", path, "creation"))
                else:
                    rows.append(_selection("device", None, "creation", valid=False))
    return rows


def registered_render_nodes(runtime) -> tuple[str, ...]:
    return tuple(dict.fromkeys(row["selector"] for row in _selections(runtime)
                               if row["kind"] == "render-node" and row["state"] != "invalid"))


def _provenance(row):
    try:
        validate_uuid(row.get("boot_id"), "boot_id")
    except UnifierError:
        return None
    if (not _device_identity(row.get("device_identity")) or not _label(row.get("backend"))
            or not _label(row.get("backend_version"))):
        return None
    return {**{key: row[key] for key in PROVENANCE_FIELDS},
            "device_identity": dict(row["device_identity"])}


def _rows_for(rows, key):
    return [row for row in rows if isinstance(row, Mapping)
            and (row.get("kind"), row.get("selector")) == key]


def _matches_device(registered, observed):
    return all(observed.get(field) == value for field, value in (registered or {}).items())


def _evidence_status(receipt, binding, state, selections, observations):
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != SCHEMA_VERSION:
        return "unverifiable", "evidence-schema"
    try:
        saved_binding = _binding(receipt.get("environment_binding"))
    except UnifierError:
        return "unverifiable", "evidence-binding"
    if binding is None:
        return "unverifiable", "environment-binding"
    if saved_binding != binding or state == "changed":
        return "stale", "environment-changed"
    if state != "matched":
        return "unverifiable", "environment-unobserved"

    key = receipt.get("kind"), receipt.get("selector")
    if key != CPU:
        matches = _rows_for(selections, key)
        if not matches and any(row["kind"] == key[0] and row["state"] != "invalid"
                               for row in selections):
            return "stale", "device-reselected"
        if key[1] is None or len(matches) != 1 or matches[0]["state"] == "invalid":
            return "unverifiable", "device-selection"
        if matches[0].get("observation") == "changed":
            return "stale", "device-changed"
        if matches[0].get("observation") != "matched":
            return "unverifiable", "device-unobserved"
    reported = _provenance(receipt)
    if not reported or not _label(receipt.get("source")):
        return "unverifiable", "evidence-provenance"
    observed = [row for row in map(_provenance, _rows_for(observations, key)) if row is not None]
    boot_ids = {row["boot_id"] for row in observed}
    if len(boot_ids) == 1 and reported["boot_id"] not in boot_ids:
        return "stale", "provenance-changed"
    expected = [row for row in observed if row["backend"] == reported["backend"]]
    if len(expected) != 1:
        return "unverifiable", "evidence-provenance"
    if reported["backend"] == "kernel-drm" and receipt.get("capability") != "drm-render-node":
        return "unverifiable", "evidence-provenance"
    if expected[0] != reported:
        return "stale", "provenance-changed"
    return "matched", None


def _classify_evidence(receipt, binding, state, selections, observations):
    if not isinstance(receipt, Mapping):
        return {"evidence_state": "unverifiable", "capability_state": "unknown",
                "result": "UNKNOWN", "reason_code": "evidence-format"}
    result = receipt.get("result")
    capability = receipt.get("capability_state")
    cleanup = receipt.get("cleanup_state")
    fallback = receipt.get("software_fallback")
    reason = receipt.get("reason_code")
    status, problem = _evidence_status(receipt, binding, state, selections, observations)
    if status == "matched":
        if (not _label(receipt.get("capability")) or capability not in CAPABILITY_STATES
                or result not in RESULTS or type(fallback) is not bool):
            status, problem = "unverifiable", "evidence-result"
        elif cleanup not in ("verified", "not-needed"):
            status, problem = "unverifiable", "cleanup-unverified"
        elif fallback:
            capability = "unknown"
            reason = "software-fallback"
        elif capability == "available" and (result != "PASS" or cleanup != "verified"):
            status, problem = "unverifiable", "capability-unverified"
        elif capability != "available" and not _label(reason):
            status, problem = "unverifiable", "capability-reason"
        elif result in ("INVALID", "UNKNOWN", "PARTIAL") and capability != "unknown":
            status, problem = "unverifiable", "capability-unverified"
    return {
        "kind": receipt.get("kind") if _label(receipt.get("kind")) else None,
        "selector": receipt.get("selector") if _label(receipt.get("selector")) else None,
        "capability": receipt.get("capability") if _label(receipt.get("capability")) else None,
        "source": receipt.get("source") if _label(receipt.get("source")) else None,
        "provenance": _provenance(receipt),
        "evidence_state": status,
        "capability_state": capability if status == "matched" else "unknown",
        "result": result if result in RESULTS else "UNKNOWN",
        "reason_code": problem or (reason if _label(reason) else None),
        "software_fallback": status == "matched" and fallback is True,
    }


def hardware_report(record, *, observed_binding=None, observations=(), evidence=(), device_errors=None) -> dict:
    """Report configuration and already gathered evidence without accessing devices.

    Observations carry boot ID, physical device identity, backend and version for
    a specific kind/selector. Only the caller can establish that they are current
    observations of the supplied binding. Evidence is accepted for that same
    binding, selection and provenance. A PASS for software remains a software
    result. The report never executes, requests access to, or opens a device.
    """
    binding, state = _registered_binding(record, observed_binding)
    runtime = record.get("runtime") if isinstance(record, dict) else None
    selections = _selections(runtime) if isinstance(runtime, dict) else []
    observations = tuple(observations)
    device_errors = device_errors or {}
    for row in selections:
        row["observation"] = "unverified"
        if state != "matched" or row["state"] == "invalid":
            continue
        key = row["kind"], row["selector"]
        if key in device_errors:
            row["reason_code"] = device_errors[key]
            row["recovery_hint"] = RECOVERY_HINTS.get(row["reason_code"])
            continue
        matches = [_provenance(item) for item in _rows_for(observations, key)]
        if matches and all(matches):
            current = matches[0]["device_identity"]
            if any(item["device_identity"] != current or item["boot_id"] != matches[0]["boot_id"]
                   for item in matches):
                continue
            same = _matches_device(row["identity"], current)
            row["observation"] = "matched" if same else "changed"
            row["observed_identity"] = current
            if not same:
                row["reason_code"] = "device-changed"
                row["recovery_hint"] = RECOVERY_HINTS["device-changed"]
    classified = [_classify_evidence(receipt, binding, state, selections, observations) for receipt in evidence]
    for item in classified:
        item["recovery_hint"] = RECOVERY_HINTS.get(item["reason_code"])
    return {
        "schema_version": SCHEMA_VERSION,
        "environment_binding": binding,
        "binding_state": state,
        "selection_state": ("unknown" if not isinstance(runtime, dict) else
                            "invalid" if any(row["state"] == "invalid" for row in selections) else
                            "selected" if selections else "none"),
        "selections": selections,
        "evidence": classified,
    }
