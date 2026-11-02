"""Use this host's existing KOS compositor bridge; never install a KWin script."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid


def request(operation, payload=None, *, snapshot=False):
    identifier = 'unifier-' + uuid.uuid4().hex
    path = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/1000')) / 'kos-platform.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall((json.dumps({'version': 1, 'requestId': identifier,
                                       'operation': operation, 'payload': payload or {}}) + '\n').encode())
        with connection.makefile('rb') as stream:
            while True:
                line = stream.readline()
                if not line:
                    raise RuntimeError('KOS bridge closed before replying')
                result = json.loads(line)
                if result.get('requestId') == identifier:
                    if result.get('ok') is not True:
                        raise RuntimeError('KOS bridge rejected the request: ' + str(result))
                    if not snapshot:
                        return result
                if snapshot and result.get('event') == 'window.snapshot':
                    return result['payload']['windows']


def window_info(directory, marker, *, activate=False, pids=(), resource_hint='', window_id=''):
    def selected():
        result = []
        for window in request('kwin.subscribe', snapshot=True):
            if not ((marker and marker in window.get('title', '')) or
                    (window['pid'] in pids and resource_hint.lower() in window.get('appId', '').lower())):
                continue
            if window_id and window['id'] != window_id:
                continue
            geometry = window.get('geometry') or {}
            content = window.get('contentGeometry') or geometry
            result.append({'caption': window.get('title'), 'resourceClass': window.get('appId'),
                           'pid': window['pid'], 'internalId': window['id'],
                           'width': geometry.get('width', 0), 'height': geometry.get('height', 0),
                           'bufferWidth': content.get('width', 0), 'bufferHeight': content.get('height', 0),
                           'active': window.get('activated', False), 'visible': window.get('visible'),
                           'observation': 'existing-KOS-KWin-bridge'})
        return result
    windows = selected()
    if activate:
        if len(windows) != 1:
            raise RuntimeError('activation requires exactly one selected application window')
        request('kwin.command', {'action': 'activate', 'id': windows[0]['internalId']})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            windows = selected()
            if len(windows) == 1 and windows[0]['active']:
                break
            time.sleep(0.1)
    return windows


def screenshot(directory, marker, output, *, pids=(), resource_hint='', window_id=''):
    before = window_info(directory, marker, activate=True, pids=pids,
                         resource_hint=resource_hint, window_id=window_id)
    if len(before) != 1 or not before[0]['active']:
        raise RuntimeError('target window was not active before capture')
    subprocess.run(['spectacle', '--new-instance', '--background', '--activewindow', '--nonotify',
                    '--no-shadow', '--output', str(output)], check=True, capture_output=True, timeout=30)
    after = window_info(directory, marker, pids=pids, resource_hint=resource_hint, window_id=window_id)
    if len(after) != 1 or after[0]['internalId'] != before[0]['internalId']:
        Path(output).unlink(missing_ok=True)
        raise RuntimeError('target window disappeared or changed during screenshot')
    return {'before': before[0], 'after': after[0], 'path': str(output),
            'focus_retained_after_capture': after[0]['active'],
            'capture': 'target active before capture and same window present afterward; image requires visual review'}


def close_owned(window, directory, hint):
    selected = window_info(directory, '', pids=[window['pid']], resource_hint=hint,
                           window_id=window['internalId'])
    if len(selected) != 1:
        raise RuntimeError('window identity changed before normal close')
    return request('kwin.command', {'action': 'close', 'id': window['internalId']})
