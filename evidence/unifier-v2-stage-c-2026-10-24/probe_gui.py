"""Open the stopped projection in real host Dolphin and KWrite windows."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import uuid

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
sys.path[:0] = [str(REPO / 'src'), str(REPO)]
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.views import ViewManager
from tools.kwin_window_probe import window_info, screenshot
import gi
from gi.repository import Gio, GLib


def close_window(window):
    """Close exactly a PID, class and KWin ID observed for our launched window."""
    token = uuid.uuid4().hex
    plugin = 'unifier-c-gui-' + token
    observer = HERE / 'gui-observer'
    observer.mkdir(exist_ok=True)
    script = observer / ('close-' + token + '.js')
    script.write_text('for (const w of workspace.windowList()) { if (w.pid === ' +
        str(window['pid']) + ' && String(w.internalId) === ' + json.dumps(window['internalId']) +
        ' && String(w.resourceClass) === ' + json.dumps(window['resourceClass']) +
        ') { w.closeWindow(); } }\n')
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    sid = bus.call_sync('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'loadScript',
          GLib.Variant('(ss)', (str(script), plugin)), GLib.VariantType.new('(i)'),
          Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
    assert sid >= 0
    try:
        bus.call_sync('org.kde.KWin', f'/Scripting/Script{sid}', 'org.kde.kwin.Script', 'run',
                      None, None, Gio.DBusCallFlags.NONE, 5000, None)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not window_info(observer, '', pids=[window['pid']], resource_hint=window['resourceClass'],
                               window_id=window['internalId']):
                return
            time.sleep(.1)
        raise RuntimeError('our read-only GUI window did not close')
    finally:
        bus.call_sync('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'unloadScript',
                      GLib.Variant('(s)', (plugin,)), None, Gio.DBusCallFlags.NONE, 5000, None)


def wait_window(marker, pids=(), hint=''):
    deadline = time.monotonic() + 20
    last = []
    while time.monotonic() < deadline:
        last = window_info(HERE / 'gui-observer', marker, pids=pids, resource_hint=hint)
        if len(last) == 1 and last[0]['width'] > 400:
            time.sleep(1)
            return last[0]
        time.sleep(.2)
    raise RuntimeError('missing own native GUI window: ' + json.dumps(last))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    fixture = json.loads((HERE / 'fixtures.json').read_text())
    p = Path(fixture['project'])
    service = EnvironmentService(p, PodmanRuntime(), tomllib.loads((p / 'config/targets.toml').read_text()),
                                 json.loads((p / 'config/images.lock.json').read_text()))
    manager = ViewManager(p, service)
    view = manager.open('view-debian13', mountpoint=str(HERE / 'gui-投影-debian13'))
    result = {'view': view, 'passed': False, 'windows': {}}
    own_windows = []
    try:
        assert service.inspect('view-debian13')['runtime_state'] == 'stopped'
        mount = Path(view['mountpoint'])
        text = (mount / 'var/lib/unifier-view/os-absolute').read_text()
        assert 'Debian GNU/Linux' in text
        for app, argv, marker, pids, hint in (
            ('dolphin', ['dolphin', '--new-window', str(mount)], mount.name, (), 'dolphin'),
            ('kwrite', ['kwrite', str(mount / 'var/lib/unifier-view/os-absolute')], '', None, 'kwrite')):
            with (HERE / (app + '-gui.log')).open('a') as log:
                child = subprocess.Popen(argv, stdout=log, stderr=log)
            ids = [child.pid] if pids is None else pids
            window = wait_window(marker, pids=ids, hint=hint)
            own_windows.append(window)
            result['windows'][app] = screenshot(HERE / 'gui-observer', '', HERE / (app + '-projection.png'),
                        pids=[window['pid']], resource_hint=hint, window_id=window['internalId'])
            assert 'XdgToplevelWindow(' in window['objectType']
            assert service.inspect('view-debian13')['runtime_state'] == 'stopped'
        result['editor_input_sha256'] = __import__('hashlib').sha256(text.encode()).hexdigest()
        result['editor_input_is_guest_debian'] = True
        result['environment_stopped_during_both_native_apps'] = True
        result['passed'] = True
    finally:
        for window in reversed(own_windows):
            close_window(window)
        manager.revoke(view['view_uuid'])
        result['revoked_after_GUI_close'] = True
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'native_apps': list(result['windows'])}))


if __name__ == '__main__':
    main()
