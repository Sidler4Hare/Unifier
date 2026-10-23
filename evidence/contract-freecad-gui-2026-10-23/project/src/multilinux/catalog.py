"""Prepared-target catalog and the upgrade/replace decision.

Debian 13, Fedora 44 and Arch are the runnable targets. A later system is
described with the same fields — family, C library, layout, package driver,
package format, runtime and update policy — and is refused at create time
until a driver for that policy exists.

Package content moves by an in-place upgrade. A change to the base image or
to any frozen create-time contract is a replacement: a new environment, with
the old one kept until the caller retires it.
"""

from multilinux.errors import MultilinuxError


CONTRACT_FIELDS = (
    "family",
    "libc",
    "layout",
    "package_driver",
    "package_format",
    "runtime_backend",
    "update_policy",
)

# The prepared experiment. These values are fixed for the three locked images.
# tools/lab.py checks config/targets.toml against this table.
PREPARED = {
    "debian13": {
        "family": "debian",
        "libc": "glibc",
        "layout": "fhs",
        "package_driver": "apt",
        "package_format": "deb",
        "runtime_backend": "podman-rootless",
        "update_policy": "versioned-release",
    },
    "fedora44": {
        "family": "fedora",
        "libc": "glibc",
        "layout": "fhs",
        "package_driver": "dnf",
        "package_format": "rpm",
        "runtime_backend": "podman-rootless",
        "update_policy": "versioned-release",
    },
    "arch": {
        "family": "arch",
        "libc": "glibc",
        "layout": "fhs",
        "package_driver": "pacman",
        "package_format": "arch",
        "runtime_backend": "podman-rootless",
        "update_policy": "full-system-upgrade",
    },
}

LIBC = {"glibc", "musl", "bionic"}
LAYOUTS = {"fhs", "nix-store", "android-system"}
RUNTIMES = {"podman-rootless", "android"}
PACKAGE_DRIVERS = {
    "apt", "dnf", "pacman", "zypper", "apk", "xbps", "portage", "nix", "android-apk",
}
PACKAGE_FORMATS = {"deb", "rpm", "arch", "apk", "xbps", "ebuild", "nix", "android-apk"}
# policy -> what a package-level change asks for. Only the first two run today.
UPDATE_POLICIES = {
    "versioned-release": "in-place",
    "full-system-upgrade": "in-place",
    "generation-switch": "switch-generation",
    "source-build": "source-build",
    "image-replace": "replace",
}
IMPLEMENTED_UPDATE = {"versioned-release", "full-system-upgrade"}
IMPLEMENTED_DRIVERS = {"apt", "dnf", "pacman"}

REPLACE_FIELDS = (
    "target",
    "image_digest",
    "user_namespace",
    "network",
    "desktop",
    "devices",
    "ipc",
    "selinux_label",
    "shares",
    "family",
    "libc",
    "layout",
    "package_driver",
    "package_format",
    "runtime_backend",
)


def describe(name: str, raw: dict | None = None) -> dict:
    """Return the catalog contract for one target.

    A prepared target keeps the values in ``PREPARED``. Any other target must
    supply the whole contract. Reserved policies and drivers are valid
    descriptions; ``require_runnable`` is what refuses to create them.
    """
    raw = dict(raw or {})
    prepared = PREPARED.get(name)
    spec = dict(prepared or {})
    for key in CONTRACT_FIELDS:
        if key not in raw or raw[key] in (None, ""):
            continue
        incoming = raw[key]
        if not isinstance(incoming, str):
            raise MultilinuxError(f"{name} {key} must be a string")
        if prepared is not None and incoming != prepared[key]:
            raise MultilinuxError(
                f"{name} {key} is fixed at {prepared[key]} for the prepared target; got {incoming!r}")
        spec[key] = incoming
    missing = [key for key in CONTRACT_FIELDS if not spec.get(key)]
    if missing:
        raise MultilinuxError(f"{name} is missing catalog fields: {', '.join(missing)}")
    _check_vocabulary(name, spec)
    return spec


def resolve_target_contract(name: str, raw: dict | None = None) -> dict:
    """The shared gate for creation, planning and native driver selection.

    A vocabulary-compatible description is not an installed target adapter.
    Only PREPARED targets currently have identity/inventory/runtime support.
    """
    if not isinstance(name, str) or not name:
        raise MultilinuxError("target must be a non-empty string")
    if raw is not None and not isinstance(raw, dict):
        raise MultilinuxError(f"{name} contract must be an object")
    spec = describe(name, raw)
    if (name not in PREPARED
            or spec["update_policy"] not in IMPLEMENTED_UPDATE
            or spec["package_driver"] not in IMPLEMENTED_DRIVERS
            or spec["runtime_backend"] != "podman-rootless"
            or spec["layout"] != "fhs"
            or spec["libc"] != "glibc"):
        raise MultilinuxError(
            f"{name} is described ({spec['package_driver']}, {spec['update_policy']}, "
            f"{spec['libc']}, {spec['layout']}, {spec['runtime_backend']}) "
            "but has no runnable upgrade driver yet")
    return spec


def require_runnable(name: str, spec: dict) -> dict:
    """Compatibility entry point; use the same gate as the actual driver."""
    return resolve_target_contract(name, spec)


def resolve_record_contract(record: dict) -> dict:
    """Validate persisted identity; only records without system use legacy defaults."""
    name = record.get("target")
    if "system" in record:
        raw = record["system"]
        if not isinstance(raw, dict):
            raise MultilinuxError(f"{name} system contract must be an object")
        missing = [key for key in CONTRACT_FIELDS if not raw.get(key)]
        if missing:
            raise MultilinuxError(f"{name} system contract missing fields: {', '.join(missing)}")
    else:
        raw = {}
    # The legacy top-level policy must not contradict the system contract.
    if "update_policy" in record:
        policy = record["update_policy"]
        if not isinstance(policy, str) or not policy:
            raise MultilinuxError(f"{name} update_policy must be a non-empty string")
        if "update_policy" in raw and raw["update_policy"] != policy:
            raise MultilinuxError(f"{name} conflicting update_policy: {policy!r} vs {raw['update_policy']!r}")
        raw = {**raw, "update_policy": policy}
    return resolve_target_contract(name, raw)


def system_contract(name: str, raw: dict | None = None) -> dict:
    """Contract stored on a newly created environment record."""
    spec = resolve_target_contract(name, raw)
    return {
        **spec,
        "package_change": "in-place",
        "base_image_change": "replace",
        "contract_change": "replace",
    }


def plan_change(record: dict, desired: dict | None = None, *,
                operation: str | None = None) -> dict:
    """Decide upgrade, replace, unchanged or refused.

    ``operation="package-change"`` is the package install/update/remove gate.
    A desired map compares frozen identity fields. Omitted keys stay as they are.
    """
    try:
        contract = resolve_record_contract(record)
    except MultilinuxError as exc:
        return {"environment_id": record.get("id"), "target": record.get("target"),
                "action": "refused", "changes": [], "reason": str(exc)}
    policy, driver = contract["update_policy"], contract["package_driver"]
    plan = {
        "environment_id": record.get("id"),
        "target": record.get("target"),
        "update_policy": policy,
        "package_driver": driver,
        "package_change": "in-place",
        "base_image_change": "replace",
        "contract_change": "replace",
    }
    if operation == "package-change":
        return {**plan, "action": "upgrade", "changes": []}
    current = _view(record, contract)
    if not desired:
        return {
            **plan,
            "action": "unchanged",
            "changes": [],
            "replace_fields": list(REPLACE_FIELDS),
            "contract": _jsonable(current),
        }
    changes = []
    for key, value in desired.items():
        if key not in current:
            raise MultilinuxError(f"unknown change field {key}")
        if _norm(current[key]) != _norm(value):
            changes.append(key)
    changes.sort()
    if not changes:
        return {**plan, "action": "unchanged", "changes": []}
    return {
        **plan,
        "action": "replace",
        "changes": changes,
        "reason": "frozen identity changes start a new environment and keep the old one",
    }


def _view(record: dict, contract: dict) -> dict:
    runtime = record.get("runtime") or {}
    base = record.get("base_image") or {}

    def pick(key: str):
        return contract[key]

    return {
        "target": record.get("target"),
        "image_digest": base.get("manifest_digest"),
        "user_namespace": runtime.get("user_namespace") or "rootless-default",
        "network": runtime.get("network") or "none",
        "desktop": record.get("desktop") is not None,
        "devices": tuple(sorted(runtime.get("devices") or ())),
        "ipc": runtime.get("ipc") or "private",
        "selinux_label": runtime.get("selinux_label") or "default",
        "shares": tuple(sorted(
            item.get("host", "") for item in (record.get("shared_mounts") or []))),
        "family": pick("family"),
        "libc": pick("libc"),
        "layout": pick("layout"),
        "package_driver": pick("package_driver"),
        "package_format": pick("package_format"),
        "runtime_backend": pick("runtime_backend"),
    }


def _norm(value):
    if isinstance(value, list):
        return tuple(value)
    return value


def _jsonable(view: dict) -> dict:
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in view.items()
    }


def _check_vocabulary(name: str, spec: dict) -> None:
    checks = (
        ("libc", LIBC),
        ("layout", LAYOUTS),
        ("runtime_backend", RUNTIMES),
        ("package_driver", PACKAGE_DRIVERS),
        ("package_format", PACKAGE_FORMATS),
        ("update_policy", set(UPDATE_POLICIES)),
    )
    for key, allowed in checks:
        if spec[key] not in allowed:
            raise MultilinuxError(f"{name} {key} {spec[key]!r} is not a known catalog value")
