"""Explicit SELinux labeling policy for ordinary shared host directories.

The default does not relabel host data and does not add ``:z``/``:Z`` to the
Podman volume. A user may ask for the historical shared relabel for one new
environment; changing that policy later requires a replacement environment.
Records created before this field existed are interpreted from their stored
mount options, and are never rewritten just by reading or planning.
"""

from multilinux.errors import MultilinuxError


PRESERVE_HOST = "preserve-host"
SHARED_RELABEL = "shared-relabel"
POLICIES = (PRESERVE_HOST, SHARED_RELABEL)
_POLICY_SET = frozenset(POLICIES)


def validate_policy(value: str, *, field: str = "share label policy") -> str:
    """Validate a policy value before any set membership or runtime use."""
    if not isinstance(value, str):
        raise MultilinuxError(f"{field} must be a string")
    if value not in _POLICY_SET:
        raise MultilinuxError(
            f"unknown {field}: {value!r}; expected one of {', '.join(POLICIES)}")
    return value


def volume_options(policy: str) -> str:
    """Return the Podman volume option text for one ordinary shared mount."""
    policy = validate_policy(policy)
    return "z" if policy == SHARED_RELABEL else ""


def infer_from_mounts(shared_mounts) -> str:
    """Historical records express the old relabel through their mount options."""
    if shared_mounts is None:
        return PRESERVE_HOST
    if not isinstance(shared_mounts, list):
        raise MultilinuxError("shared_mounts must be a list")
    relabel = False
    for item in shared_mounts:
        if not isinstance(item, dict):
            raise MultilinuxError("shared_mounts entries must be objects")
        options = item.get("options", "")
        if not isinstance(options, str):
            raise MultilinuxError("shared_mounts options must be a string")
        tokens = {token.strip() for token in options.split(",") if token.strip()}
        if "z" in tokens or "Z" in tokens:
            relabel = True
    return SHARED_RELABEL if relabel else PRESERVE_HOST


def effective_policy(record: dict) -> str:
    """Return the policy that governs an existing record, without writing it.

    A persisted policy wins, but it must not contradict the stored per-mount
    options. Missing policy on an old record is inferred from those options so
    an old ``:z`` environment remains ``shared-relabel`` for planning.
    """
    if not isinstance(record, dict):
        raise MultilinuxError("environment record must be an object")
    runtime = record.get("runtime")
    if runtime is None:
        runtime = {}
    if not isinstance(runtime, dict):
        raise MultilinuxError("environment runtime record must be an object")
    shares = record.get("shared_mounts")
    inferred = infer_from_mounts(shares)
    if "share_label_policy" in runtime:
        policy = validate_policy(runtime["share_label_policy"])
        if shares:
            if inferred != policy:
                raise MultilinuxError(
                    "environment share_label_policy conflicts with shared_mounts options")
        return policy
    return inferred
