"""Installed menu -> visible native window -> normal close -> automatic stop.

The review marker is written by the verifier after inspecting the screenshot;
it is an internal checkpoint, not a user confirmation or a forced-stop path.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

RUN = Path(__file__).resolve().parent
CTX = json.loads((RUN / 'context.json').read_text())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', choices=['freecad', 'wemeet'], required=True)
    parser.add_argument('--attempt', type=int, default=1)
    args = parser.parse_args()
    app = args.app
    spec = CTX['apps'][app]
    root = Path(spec['root'])
    sys.path[:0] = [str(root / 'src'), str(root), str(RUN.parents[1])]
    from kos_windows import window_info, screenshot, close_owned
    from multilinux.cli import default_service
    service = default_service()
    assert service.root == root
    directory = RUN / (app + '-menu' + ('' if args.attempt == 1 else f'-attempt{args.attempt}'))
    directory.mkdir(exist_ok=False)
    cid, env_id = spec['container_id'], spec['environment']
    hint = 'freecad' if app == 'freecad' else 'wemeet'
    leases_path = root / '.lab/leases' / (env_id + '.json')
    results = {'status': 'RUNNING', 'container_id': cid, 'cycles': [],
               'scope': 'Installed menu, visible window, normal WM close and auto-stop; no login or meeting/media acceptance'}
    try:
        for cycle in [1, 2]:
            view = service.inspect(env_id)
            assert view['runtime_state'] == 'stopped'
            assert view['compatibility']['allowed']
            assert view['compatibility']['mode'] == 'whitelist'
            assert view['runtime']['container_id'] == cid
            assert hashlib.sha256(Path(spec['desktop']).read_bytes()).hexdigest() == spec['desktop_sha256']
            started = time.monotonic()
            with (directory / f'launch-{cycle}.log').open('w') as stream:
                launched = subprocess.run(['gtk-launch', Path(spec['desktop']).stem], stdout=stream, stderr=stream, timeout=15)
            assert launched.returncode == 0
            stable_id, stable_since, window = None, started, None
            while time.monotonic() - started < 80:
                listing = subprocess.run(['podman', 'top', cid, 'hpid', 'comm'], capture_output=True, text=True)
                pids = [int(row.split()[0]) for row in listing.stdout.splitlines()[1:]
                        if row.split() and row.split()[0].isdigit()]
                windows = window_info(directory / 'observer', '', pids=pids, resource_hint=hint) if pids else []
                windows = [w for w in windows if w['width'] > 300 and w['height'] > 200
                           and w['bufferWidth'] > 0 and w['bufferHeight'] > 0]
                if windows:
                    window = max(windows, key=lambda item: item['width'] * item['height'])
                    if window['internalId'] != stable_id:
                        stable_id, stable_since = window['internalId'], time.monotonic()
                    if time.monotonic() - stable_since >= 3 and time.monotonic() - started >= (12 if app == 'freecad' else 8):
                        break
                time.sleep(0.3)
            assert window is not None, 'no visible application window'
            shot = screenshot(directory / 'observer', '', directory / f'launch-{cycle}.png',
                              pids=[window['pid']], resource_hint=hint, window_id=window['internalId'])
            leases = json.loads(leases_path.read_text())
            assert any(item.get('app_id') == app for item in leases['leases'].values())
            current = service.inspect(env_id)
            assert current['runtime_state'] == 'running' and current['runtime']['container_id'] == cid
            item = {'cycle': cycle, 'window': window, 'screenshot': shot, 'leases_during': leases,
                    'compatibility_during': current['compatibility'], 'container_id_preserved': True}
            results['cycles'].append(item)
            save(directory / 'result.json', results)
            print(json.dumps({'event': 'window-ready-for-review', 'app': app, 'cycle': cycle,
                              'screenshot': shot['path']}), flush=True)
            marker = directory / f'reviewed-{cycle}'
            deadline = time.monotonic() + 180
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            assert marker.exists(), 'screenshot review checkpoint timed out; application left intact'
            close_owned(window, directory / 'observer', hint)
            closed = time.monotonic()
            deadline = closed + 45
            while time.monotonic() < deadline:
                view = service.inspect(env_id)
                remaining = json.loads(leases_path.read_text())
                if view['runtime_state'] == 'stopped' and not remaining['leases']:
                    break
                time.sleep(0.2)
            item.update(state_after_close=view['runtime_state'], leases_after=remaining,
                        close_to_stop_seconds=time.monotonic() - closed, normal_wm_close=True)
            assert view['runtime_state'] == 'stopped' and not remaining['leases'], 'normal close did not auto-stop'
            assert not window_info(directory / 'observer', '', pids=[window['pid']],
                                   resource_hint=hint, window_id=window['internalId'])
            save(directory / 'result.json', results)
            print(json.dumps({'event': 'cycle-passed', 'app': app, 'cycle': cycle,
                              'automatic_stop': True}), flush=True)
        results['status'] = 'PASSED_MENU_NORMAL_CLOSE_AND_AUTOSTOP'
    except BaseException as exc:
        results['status'] = 'FAILED'
        results['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        save(directory / 'result.json', results)


if __name__ == '__main__':
    main()
