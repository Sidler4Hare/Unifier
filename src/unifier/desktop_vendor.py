"""Native desktop exceptions retained for verified application workflows."""

from unifier import wps_portal


APPLICATION_CLIENTS = {
    "debian13": {"com.alibabainc.dingtalk": "gtk3", "wemeet": "pipewire"},
    "arch": {"freecad": "qt6"},
}

# The shared proxy starts before the eventual meeting application. Keep its
# vendor name ownership rule until a real sharing and teardown run passes
# without this rule.
SHARED_BRIDGE_OWN_NAMES = ("com.tencent.wemeet.*",)

_WPS_PREREQUISITES = (
    "bsdextrautils", "libasound2t64", "libcurl4t64", "libgbm1",
    "libgl1", "libgtk-3-0t64", "libnss3", "libx11-xcb1", "libxtst6",
)


def local_install_prerequisites(target, packages):
    # The Debian build requires hexdump during postinst and these libraries
    # for its own Qt, document and CEF components.
    if target == "debian13" and "wps-office" in packages:
        return _WPS_PREREQUISITES
    return ()


def application_exit_policy(target, package, argv):
    # The Writer launcher waits for its main window. Owned CEF/cloud helpers
    # can remain after that window is closed.
    if target == "debian13" and package == "wps-office" and argv == ["/usr/bin/wps"]:
        return "stop-children"
    return None


def portal_picker_mode(record, executable, desktop_id):
    if desktop_id == "org.freecad.FreeCAD" or executable == "/usr/bin/freecad":
        return "qt-portal"
    if wps_portal.is_writer(record, executable) and wps_portal.payload_available():
        return wps_portal.MODE
    if (desktop_id in ("com.alibabainc.dingtalk", "dingtalk")
            or executable == "/opt/apps/com.alibabainc.dingtalk/files/Elevator.sh"):
        return "gtk-native-portal"
    return None
