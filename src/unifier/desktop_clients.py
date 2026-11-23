"""Native guest packages for clients of the host desktop services."""

from unifier.desktop_vendor import APPLICATION_CLIENTS
from unifier.errors import UnifierError


CLIENTS = ("gtk3", "qt6", "pipewire")
PACKAGES = {
    "debian13": {
        "gtk3": "libgtk-3-0t64",
        "qt6": "qt6-xdgdesktopportal-platformtheme",
        "pipewire": "pipewire-bin",
    },
    "arch": {"gtk3": "gtk3", "qt6": "qt6-base", "pipewire": "pipewire"},
    "fedora44": {"gtk3": "gtk3", "qt6": "qt6-qtbase-gui", "pipewire": "pipewire-libs"},
}

def desktop_provisioning_failure(record):
    """Return unfinished initial client provisioning, if the record scheduled it."""
    state = record.get("desktop_client_provisioning")
    if state is None:
        return None
    if not isinstance(state, dict) or state.get("phase") not in (
            "pending", "failed", "needs-recovery", "completed"):
        raise UnifierError("invalid desktop client provisioning record")
    if state["phase"] != "completed":
        return state
    return None


def desktop_repair_command(env_id):
    return f"unifier env desktop-integration {env_id} --repair"


def application_clients(record, packages):
    desktop = record.get("desktop")
    if not desktop:
        return ()
    profiles = APPLICATION_CLIENTS.get(record["target"], {})
    requested = {profiles[name] for name in packages if name in profiles}
    if not desktop.get("portal_proxy"):
        # Wemeet also needs the client for its existing desktop media path.
        requested.intersection_update({"pipewire"})
    return tuple(client for client in CLIENTS if client in requested)


def client_packages(target, clients):
    providers = PACKAGES.get(target, {})
    try:
        return {client: providers[client] for client in clients}
    except KeyError as exc:
        raise UnifierError(f"no desktop client package for {exc.args[0]} on {target}") from exc
