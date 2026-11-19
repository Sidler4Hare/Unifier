"""Native guest packages for clients of the host desktop services."""

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

# Match known package builds. A Qt 5 application, including Debian's
# FreeCAD, cannot use the native Qt 6 platform-theme plugin.
APPLICATION_CLIENTS = {
    "debian13": {"com.alibabainc.dingtalk": "gtk3", "wemeet": "pipewire"},
    "arch": {"freecad": "qt6"},
}


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
