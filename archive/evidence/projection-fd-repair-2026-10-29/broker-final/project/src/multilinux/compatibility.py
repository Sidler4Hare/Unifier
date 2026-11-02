"""Explicit host admission and host/guest relationships for the container MVP.

A family shares an integration strategy, never repositories, an ABI, a package
database or an environment identity. ID_LIKE is diagnostic only. Host admission
does not install another guest adapter or prove a runtime/application works.
"""

from pathlib import Path
import platform
import shlex

from multilinux.catalog import PREPARED, resolve_target_contract
from multilinux.errors import MultilinuxError


POLICY_VERSION = 1
FAMILIES = {
    "debian": {
        "ids": ("debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin"),
        "package_driver": "apt", "package_format": "deb",
        "host_admitted": True, "reference_host": "debian",
    },
    "fedora": {
        "ids": ("fedora",), "package_driver": "dnf", "package_format": "rpm",
        "host_admitted": True, "reference_host": "fedora",
    },
    "rhel": {
        "ids": ("rhel", "centos", "rocky", "almalinux"),
        "package_driver": "dnf", "package_format": "rpm",
        "host_admitted": True, "reference_host": "rhel",
    },
    "arch": {
        "ids": ("arch", "endeavouros", "manjaro"),
        "package_driver": "pacman", "package_format": "arch",
        "host_admitted": True, "reference_host": "arch",
    },
    "suse": {
        "ids": ("opensuse-leap", "opensuse-tumbleweed", "sles", "sled"),
        "package_driver": "zypper", "package_format": "rpm",
        "host_admitted": True, "reference_host": "opensuse-tumbleweed",
    },
    "alpine": {
        "ids": ("alpine",), "package_driver": "apk", "package_format": "apk",
        "host_admitted": False, "reason": "musl host integration needs a separate adapter",
    },
    "nixos": {
        "ids": ("nixos",), "package_driver": "nix", "package_format": "nix",
        "host_admitted": False, "reason": "Nix store and generation integration need a separate adapter",
    },
    "void": {
        "ids": ("void",), "package_driver": "xbps", "package_format": "xbps",
        "host_admitted": False, "reason": "XBPS, runit and libc variants need a separate adapter",
    },
    "gentoo": {
        "ids": ("gentoo",), "package_driver": "portage", "package_format": "ebuild",
        "host_admitted": False, "reason": "source builds, profiles and init variants need a separate adapter",
    },
}
ATOMIC_VARIANTS = {
    "silverblue", "kinoite", "sericea", "onyx", "coreos", "iot",
    "sway-atomic", "budgie-atomic", "cosmic-atomic", "xfce-atomic",
    "microos", "aeon", "kalpa", "micro", "atomic", "bootc",
}
REQUIREMENTS = (
    "rootless-podman", "cgroup-v2", "locked-linux-amd64-image",
    "guest-identity-and-shared-kernel-check", "host-security-policy",
    "desktop-session-and-devices-if-requested",
)


def compatibility_mode(config: dict) -> str:
    section = config.get("compatibility", {})
    if not isinstance(section, dict) or section.get("mode", "whitelist") != "whitelist":
        raise MultilinuxError("compatibility.mode must be whitelist")
    return "whitelist"


def observe_host(os_release_path: Path | None = None) -> dict:
    """Observe without a shell, package changes, runtime calls or system writes."""
    if os_release_path is None:
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
    else:
        release = {}
        for line in Path(os_release_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            try:
                parts = shlex.split(value, comments=True)
            except ValueError as exc:
                raise MultilinuxError(f"invalid os-release value for {key}: {exc}") from exc
            if len(parts) > 1:
                raise MultilinuxError(f"invalid os-release value for {key}")
            release[key] = parts[0] if parts else ""
    return {
        "os_release": release, "system": platform.system(),
        "machine": platform.machine(), "libc": platform.libc_ver()[0],
        "ostree": Path("/run/ostree-booted").exists(),
        "transactional": any(Path(p).exists() for p in (
            "/usr/sbin/transactional-update", "/usr/bin/transactional-update")),
        "observation": "live-host" if os_release_path is None else "release-file-simulation",
    }


def classify_host(facts: dict) -> dict:
    if not isinstance(facts, dict) or not isinstance(facts.get("os_release"), dict):
        raise MultilinuxError("host facts must contain an os_release object")
    release = facts["os_release"]
    for key in ("ID", "ID_LIKE", "VERSION_ID", "VARIANT_ID"):
        if not isinstance(release.get(key, ""), str):
            raise MultilinuxError(f"host os-release {key} must be a string")
    for key in ("ostree", "transactional"):
        if key in facts and not isinstance(facts[key], bool):
            raise MultilinuxError(f"host fact {key} must be a boolean")
    distro = release.get("ID", "")
    family = next((name for name, group in FAMILIES.items() if distro in group["ids"]), None)
    group = FAMILIES.get(family, {})
    variant = release.get("VARIANT_ID", "")
    atomic = (facts.get("ostree") is True or facts.get("transactional") is True
              or variant in ATOMIC_VARIANTS or variant.endswith("-atomic"))
    reasons = []
    if not group:
        reasons.append(f"distribution ID {distro!r} is not in the explicit host whitelist")
    elif not group["host_admitted"]:
        reasons.append(group["reason"])
    if atomic:
        reasons.append("atomic/transactional host requires a separate integration profile")
    if facts.get("system") != "Linux" or facts.get("machine") != "x86_64":
        reasons.append("the initial runtime requires a Linux x86_64 host")
    if facts.get("libc") != "glibc":
        reasons.append("the initial host integration requires observed glibc")
    return {
        "id": distro, "version_id": release.get("VERSION_ID", ""),
        "variant_id": variant, "id_like": release.get("ID_LIKE", "").split(),
        "family": family, "reference_host": group.get("reference_host"),
        "integration_profile": (f"{family}-atomic" if atomic and family else family),
        "package_driver": None if atomic else group.get("package_driver"),
        "base_package_driver": group.get("package_driver"),
        "package_format": group.get("package_format"),
        "system": facts.get("system"), "machine": facts.get("machine"),
        "libc": facts.get("libc"), "atomic": atomic,
        "admitted": not reasons, "reasons": reasons,
        "observation": facts.get("observation", "injected-facts"),
    }


def relationship_plan(facts: dict, target: str, raw: dict | None = None) -> dict:
    host = classify_host(facts)
    reasons = list(host["reasons"])
    try:
        contract = resolve_target_contract(target, raw)
    except MultilinuxError as exc:
        contract = None
        reasons.append(str(exc))
    guest_family = contract["family"] if contract else None
    if reasons:
        relationship = "unsupported"
    elif host["family"] != guest_family:
        relationship = "cross-family"
    elif host["id"] == {"debian13": "debian", "fedora44": "fedora", "arch": "arch"}[target]:
        relationship = "same-distribution"
    else:
        relationship = "same-family"
    return {
        "schema_version": 1, "policy_version": POLICY_VERSION, "mode": "whitelist",
        "host": host, "target": target, "container_family": guest_family,
        "container_contract": contract, "allowed": not reasons,
        "relationship": relationship, "reasons": reasons,
        "execution_domain": "isolated-container", "host_authority": True,
        "shared_kernel": True, "share_system_trees": False,
        "share_package_databases": False,
        "native_package_validation": "required-even-within-family",
        "requirements": list(REQUIREMENTS), "validation_scope": "policy-only",
    }


def require_relationship(plan: dict) -> dict:
    if not plan["allowed"]:
        raise MultilinuxError("compatibility whitelist refused: " + "; ".join(plan["reasons"]))
    return plan


def family_catalog() -> dict:
    return {
        "schema_version": 1, "policy_version": POLICY_VERSION, "mode": "whitelist",
        "families": FAMILIES,
        "container_targets": {name: spec["family"] for name, spec in PREPARED.items()},
        "atomic_variants_excluded": sorted(ATOMIC_VARIANTS),
        "id_like_grants_admission": False, "validation_scope": "policy-only",
    }
