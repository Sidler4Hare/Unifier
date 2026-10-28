"""Launch the installed desktop ID twice; inspect only its own native windows."""
from pathlib import Path
import json
import subprocess
import sys
import time
import tomllib
import uuid

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
DEPLOY = json.loads((HERE / 'deployment.json').read_text())
APP = Path(DEPLOY['app_root'])
sys.path[:0] = [str(APP / 'src'), str(APP), str(REPO)]
import gi
from gi.repository import Gio, GLib
from tools.kwin_window_probe import window_info, screenshot
from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService


def close_owned(window):
    """Request WM close only for our exact CID's observed host PID/window ID."""
    token = uuid.uuid4().hex
    plugin = 'unifier-freecad-menu-test-' + token
    script = HERE / 'observer' / ('close-' + token + '.js')
    script.parent.mkdir(exist_ok=True)
    script.write_text('for (const w of workspace.windowList()) {\n' +
        ' if (w.pid === ' + str(window['pid']) + ' && String(w.internalId) === ' +
        json.dumps(window['internalId']) +
        ' && String(w.resourceClass).toLowerCase().indexOf("freecad") !== -1) { w.closeWindow(); }\n}\n')
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    sid = bus.call_sync('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'loadScript',
          GLib.Variant('(ss)', (str(script), plugin)), GLib.VariantType.new('(i)'),
          Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
    assert sid >= 0
    try:
        bus.call_sync('org.kde.KWin', f'/Scripting/Script{sid}', 'org.kde.kwin.Script',
                      'run', None, None, Gio.DBusCallFlags.NONE, 5000, None)
        time.sleep(.2)
    finally:
        bus.call_sync('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'unloadScript',
                      GLib.Variant('(s)', (plugin,)), None, Gio.DBusCallFlags.NONE, 5000, None)


def main():
    result_file = HERE / 'menu-validation.json'
    if result_file.exists():
        raise RuntimeError('choose a new evidence result for a rerun')
    config = tomllib.loads((APP / 'config/targets.toml').read_text())
    lock = json.loads((APP / 'config/images.lock.json').read_text())
    service = EnvironmentService(APP, PodmanRuntime(), config, lock)
    cid = DEPLOY['environment']['runtime']['container_id']
    cached = Gio.DesktopAppInfo.new('unifier-freecad.desktop')
    assert cached and cached.get_name() == 'FreeCAD'
    result = {'desktop': cached.get_filename(), 'desktop_discoverable': True, 'sessions': []}
    for number in (1, 2):
        assert service.inspect('freecad-arch')['runtime_state'] == 'stopped'
        # A desktop child can inherit stdout after gtk-launch exits. Regular
        # files let us observe the launcher exit without waiting for GUI EOF.
        with (HERE / f'menu-launch-{number}.log').open('a') as log:
            launch = subprocess.run(['gtk-launch', 'unifier-freecad'], stdout=log, stderr=log, timeout=15)
        assert launch.returncode == 0
        deadline = time.monotonic() + 60
        launched_at = time.monotonic()
        stable_id = None
        stable_since = launched_at
        windows = []
        while time.monotonic() < deadline:
            top = subprocess.run(['podman', 'top', cid, 'hpid', 'comm'], capture_output=True, text=True)
            pids = [int(line.split()[0]) for line in top.stdout.splitlines()[1:]
                    if line.split() and line.split()[0].isdigit()]
            windows = window_info(HERE / 'observer', '', pids=pids, resource_hint='freecad')
            windows = [w for w in windows if w['width'] > 400 and w['height'] > 300]
            if len(windows) == 1:
                if windows[0]['internalId'] != stable_id:
                    stable_id = windows[0]['internalId']
                    stable_since = time.monotonic()
                # Do not accept the transient splash/initialization window.
                if time.monotonic() - launched_at >= 18 and time.monotonic() - stable_since >= 4:
                    break
            time.sleep(.3)
        assert len(windows) == 1, windows
        window = windows[0]
        assert 'XdgToplevelWindow(' in window['objectType']
        shot = screenshot(HERE / 'observer', '', HERE / f'menu-launch-{number}.png',
                           pids=[window['pid']], resource_hint='freecad', window_id=window['internalId'])
        lease_file = APP / '.lab/leases/freecad-arch.json'
        leases = json.loads(lease_file.read_text())
        assert leases['leases']
        close_owned(window)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = service.inspect('freecad-arch')['runtime_state']
            remaining = json.loads(lease_file.read_text())
            if state == 'stopped' and not remaining.get('leases'):
                break
            time.sleep(.2)
        assert state == 'stopped' and not remaining.get('leases')
        result['sessions'].append({'number': number, 'menu_exit': launch.returncode,
               'window': window, 'screenshot': shot, 'leases_during': leases,
               'stopped_after_close': True, 'leases_after': remaining,
               'same_container': cid == service.store.load('freecad-arch')['runtime']['container_id']})
    result.update(passed=True, persistent_container_retained=True, final_state='stopped',
                  native_menu_entry_retained=Path(cached.get_filename()).is_file(),
                  no_test_macro_in_application=True)
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'native_menu_launches': 2, 'final_state': 'stopped'}))


if __name__ == '__main__':
    main()
