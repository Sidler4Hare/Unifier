#!/usr/bin/env python3
"""Native Wayland, clipboard, audio and local Debian package acceptance.

All work uses a new persistent project snapshot and native package managers.
The previous XWayland experiment and the main project's old environments are
not started or modified. Installed environments are retained stopped.
"""

import argparse
import array
import base64
from datetime import datetime
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
import traceback
import wave
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.probe_desktop_apps import prepare, save, service_for, sha, log, need
from multilinux.service import now
from multilinux.packages import HostObserver
from multilinux.resources import ResourceProbe
from tools.kwin_window_probe import window_info, screenshot

sys.path.insert(0, str(ROOT / 'fixtures'))
from wayland_clipboard import Clipboard

PACKAGES = {
    "debian13": ["mousepad", "wl-clipboard", "dbus", "python3-gi", "gir1.2-gtk-3.0",
                 "pulseaudio-utils", "fonts-noto-cjk"],
    "fedora44": ["mousepad", "wl-clipboard", "dbus-daemon", "python3-gobject",
                 "pulseaudio-utils", "google-noto-sans-cjk-vf-fonts"],
    "arch": ["mousepad", "wl-clipboard", "dbus", "python-gobject", "gtk3", "libpulse",
             "noto-fonts-cjk"],
}


class ClipboardGuard:
    def __enter__(self):
        clipboard = Clipboard()
        try:
            self.original = {mime: clipboard.read(mime) for mime in clipboard.types()}
        finally:
            clipboard.close()
        self.restored = False
        return self

    def __exit__(self, *exception):
        if not self.original:
            c = Clipboard(); c.own({}); c.close()
        else:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / 'fixtures/wayland_clipboard.py'), 'restore'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True)
            data = {mime: base64.b64encode(body).decode() for mime, body in self.original.items()}
            process.stdin.write(json.dumps(data).encode()); process.stdin.close()
            need(select.select([process.stdout], [], [], 8)[0], 'clipboard restoration owner timed out')
            need(process.stdout.readline().strip() == b'clipboard-owner-ready', 'clipboard restore failed')
            process.stdout.close()
            c = Clipboard()
            try:
                need(set(self.original).issubset(c.types()), 'original clipboard MIME formats were not restored')
                need(all(c.read(mime) == body for mime, body in self.original.items()), 'original clipboard data did not restore')
            finally:
                c.close()
        self.restored = True
        self.original.clear()


def host_paste():
    c = Clipboard()
    try:
        return c.read('text/plain;charset=utf-8')
    finally:
        c.close()


def host_copy(path):
    p = subprocess.Popen([sys.executable, str(ROOT / 'fixtures/wayland_clipboard.py'),
                          'copy', '--file', str(path)], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    need(select.select([p.stdout], [], [], 8)[0], 'host clipboard owner timed out')
    need(p.stdout.readline().strip() == b'clipboard-owner-ready', 'host clipboard owner failed')
    return p


def app_exec(service, app_id, argv, *, timeout=30):
    result = service.exec_app(app_id, argv, capture=True, timeout=timeout)
    need(result.returncode == 0, f'{argv[0]} exited {result.returncode}: {result.stderr[-1600:]}')
    return result


def control(service, app_id, docs, operation, action=None):
    argv = ['python3', str(docs / 'mousepad_control.py'), str(docs / 'bus-address'), operation]
    if action:
        argv.append(action)
    return json.loads(app_exec(service, app_id, argv).stdout)


def wait_window(directory, marker, process):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        need(process.poll() is None, f'native editor launcher exited {process.returncode}')
        found = window_info(directory, marker, activate=True)
        if len(found) == 1:
            need((found[0].get('wayland') is True and found[0].get('x11') is False)
                 or 'KWin::XdgToplevelWindow' in found[0].get('objectType', ''),
                 f'editor is not a verified native Wayland window: {found}')
            return found[0]
        time.sleep(0.5)
    raise RuntimeError('native editor window did not appear')


def launch_editor(run_dir, app_id, note, log_path):
    with log_path.open('w') as stream:
        return subprocess.Popen([sys.executable, str(run_dir / 'project/tools/ml.py'), 'app',
                                 'open', app_id, '--', str(note)], stdout=stream,
                                stderr=subprocess.STDOUT, cwd=run_dir / 'project')


def pulse(kind):
    argv = ['pactl', '-f', 'json', 'list', kind]
    r = subprocess.run(argv, capture_output=True, text=True, check=True,
                       env={**os.environ, 'LC_ALL': 'C.UTF-8'}, timeout=10)
    return json.loads(r.stdout)


def audio_test(service, app_id, docs, directory, frequency):
    tone = docs / 'tone.wav'
    rate, seconds = 48000, 3
    samples = array.array('h', [int(2200 * min(1, i / 2400, (rate * seconds - i) / 2400)
                                  * math.sin(2 * math.pi * frequency * i / rate))
                                for i in range(rate * seconds)])
    with wave.open(str(tone), 'wb') as output:
        output.setparams((1, 2, rate, len(samples), 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())
    tag = 'multilinux-tone-' + directory.parent.name
    player_log = (directory / 'paplay.log').open('w')
    record_log = (directory / 'parec.log').open('w')
    capture = directory / 'own-playback.pcm'
    player = subprocess.Popen([sys.executable, str(service.root / 'tools/ml.py'), 'app', 'exec',
                               app_id, '--', 'paplay', '--client-name=' + tag,
                               '--property=application.name=' + tag, str(tone)],
                              stdout=player_log, stderr=subprocess.STDOUT)
    recorder = None
    try:
        stream = None
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            found = [s for s in pulse('sink-inputs') if s['properties'].get('application.name') == tag]
            if found:
                stream = found[0]; break
            need(player.poll() is None, 'audio player exited before a playback stream was found')
            time.sleep(0.03)
        need(stream is not None, 'no live playback stream from environment')
        sink = next(s for s in pulse('sinks') if s['index'] == stream['sink'])
        need(not stream['mute'] and not stream['corked'] and not sink['mute'], 'audio playback is muted or corked')
        need(sink['properties'].get('device.api') in ('alsa', 'bluez5'), 'playback did not reach a hardware output route')
        with capture.open('wb') as recorded:
            recorder = subprocess.Popen(['parec', '--device=' + sink['monitor_source'],
                                         '--monitor-stream=' + str(stream['index']), '--raw',
                                         '--format=s16le', '--rate=48000', '--channels=1'],
                                        stdout=recorded, stderr=record_log)
            need(player.wait(timeout=12) == 0, 'paplay did not finish successfully')
            time.sleep(0.2)
            recorder.terminate(); recorder.wait(timeout=5)
        body = capture.read_bytes()
        pcm = array.array('h'); pcm.frombytes(body[:len(body) - len(body) % 2])
        nonzero = [i for i, value in enumerate(pcm) if abs(value) > 4]
        need(len(nonzero) > rate // 2, 'no substantial played signal was recorded from the own-stream monitor')
        signal = pcm[nonzero[0]:nonzero[-1] + 1]
        energy = sum(x * x for x in signal)
        sine = sum(x * math.sin(2 * math.pi * frequency * i / rate) for i, x in enumerate(signal))
        cosine = sum(x * math.cos(2 * math.pi * frequency * i / rate) for i, x in enumerate(signal))
        ratio = 2 * (sine * sine + cosine * cosine) / (len(signal) * energy)
        need(ratio > 0.75, f'captured playback did not match the known tone: spectral fraction {ratio}')
        result = {'passed': True, 'frequency_hz': frequency, 'tone_sha256': sha(tone),
                  'capture_sha256': sha(capture), 'capture_bytes': len(body), 'rate': rate,
                  'nonzero_samples': len(nonzero), 'rms': math.sqrt(energy / len(signal)),
                  'tone_energy_fraction': ratio, 'sink_input_index': stream['index'],
                  'sink_index': sink['index'], 'sink_description': sink['description'],
                  'device_api': sink['properties']['device.api'], 'sink_state': sink['state'],
                  'sink_muted': sink['mute'], 'sink_volume': sink['volume'],
                  'capture_scope': 'own sink-input monitor only; no microphone or other application audio',
                  'physical_audibility': 'not independently confirmed by human listening'}
        save(directory / 'audio.json', result)
        return result
    finally:
        if player.poll() is None:
            player.terminate(); player.wait(timeout=8)
        if recorder is not None and recorder.poll() is None:
            recorder.terminate(); recorder.wait(timeout=5)
        player_log.close(); record_log.close()


def run_native_target(run_dir, manifest, target):
    service = service_for(run_dir)
    env_id, app_id = 'native-' + target, 'native-editor-' + target
    docs = run_dir / 'documents' / target
    entry = manifest['targets'][target]
    attempts = entry.setdefault('native_attempts', [])
    directory = run_dir / target / ('native-attempt-' + str(len(attempts) + 1))
    directory.mkdir()
    for name in ('launch_mousepad.py', 'mousepad_control.py', 'wayland_clipboard.py'):
        shutil.copyfile(ROOT / 'fixtures' / name, docs / name)
        shutil.copyfile(ROOT / 'fixtures' / name, directory / name)
    shutil.copyfile(__file__, directory / 'driver.py')
    note = docs / ('Wayland 实测 ' + target + '.txt')
    initial = f'Native original file: {target}\n'
    note.write_text(initial, encoding='utf-8')
    payload = (f'Multilinux native Wayland: {target}\n中文剪贴板粘贴、保存与重开。\n'
               'Host to editor, then editor to host.\n').encode('utf-8')
    payload_file = docs / 'clipboard-test.txt'; payload_file.write_bytes(payload)
    attempt = {'started_at': now(), 'stage': 'launch', 'passed': False, 'errors': [],
               'artifacts': str(directory.relative_to(run_dir)), 'driver_sha256': sha(__file__)}
    attempts.append(attempt)
    process = owner = None
    try:
        if not service._launch_exists(app_id):
            exe = '/usr/bin/dbus-run-session'
            service.add_app(env_id, app_id, exe,
                            [exe, '--', '/usr/bin/python3', str(docs / 'launch_mousepad.py'),
                             str(docs / 'bus-address')], workdir=str(docs),
                            env_overlay={'GDK_BACKEND': 'wayland', 'LC_ALL': 'C.UTF-8'})
        log(f'{target}: launching native Wayland Mousepad')
        process = launch_editor(run_dir, app_id, note, directory / 'first-open.log')
        attempt['window'] = wait_window(directory, note.name, process)
        actions = control(service, app_id, docs, 'list')
        save(directory / 'actions.json', actions)
        attempt['stage'] = 'native-gui-clipboard-save'
        owner = host_copy(payload_file)
        control(service, app_id, docs, 'action', 'edit.select-all')
        control(service, app_id, docs, 'action', 'edit.paste')
        need(note.read_text() == initial, 'disk changed before the GUI save action')
        control(service, app_id, docs, 'action', 'edit.select-all')
        control(service, app_id, docs, 'action', 'edit.copy')
        need(host_paste() == payload, 'native GUI copy/paste round trip differs from host text')
        control(service, app_id, docs, 'action', 'file.save')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and note.read_bytes() != payload:
            time.sleep(0.1)
        need(note.read_bytes() == payload, 'native GUI saved bytes differ from clipboard text')
        need(note.stat().st_uid == os.getuid() and note.stat().st_gid == os.getgid(), 'native saved file owner mismatch')
        attempt['saved_window'] = screenshot(directory, note.name, directory / 'saved.png')
        save(directory / 'gui-resource.json', ResourceProbe(service).sample(env_id))
        control(service, app_id, docs, 'action', 'file.close-window')
        need(process.wait(timeout=20) == 0, 'native GUI did not exit cleanly')
        process = None
        service.stop(env_id)
        need(service.inspect(env_id)['runtime_state'] == 'stopped', 'environment did not stop')
        attempt['stage'] = 'native-reopen'
        process = launch_editor(run_dir, app_id, note, directory / 'reopen.log')
        attempt['reopened_window'] = wait_window(directory, note.name, process)
        control(service, app_id, docs, 'action', 'edit.select-all')
        control(service, app_id, docs, 'action', 'edit.copy')
        need(host_paste() == payload and note.read_bytes() == payload, 'reopened native GUI buffer does not match disk')
        attempt['reopened_screenshot'] = screenshot(directory, note.name, directory / 'reopened.png')
        control(service, app_id, docs, 'action', 'file.close-window')
        need(process.wait(timeout=20) == 0, 'reopened native GUI did not exit cleanly')
        process = None
        trace = (directory / 'first-open.log').read_text()
        need('xdg_toplevel' in trace and 'wl_surface' in trace and '.attach(' in trace,
             'missing native Wayland toplevel/buffer protocol evidence')
        attempt['wayland_protocol'] = {'xdg_toplevel': True, 'surface_buffer_attached': True,
                                       'trace_sha256': sha(directory / 'first-open.log')}
        attempt['document_sha256'] = sha(note)
        attempt['clipboard'] = {'host_to_native_gui': True, 'native_gui_to_host': True,
                                'reopened_buffer_to_host': True, 'utf8_bytes': len(payload)}
        attempt['stage'] = 'actual-audio-playback'
        frequency = {'debian13': 523, 'fedora44': 659, 'arch': 784}[target]
        log(f'{target}: playing and verifying {frequency} Hz on the current audio output')
        attempt['audio'] = audio_test(service, app_id, docs, directory, frequency)
        attempt['passed'] = True
        attempt['stage'] = 'completed'
        log(f'{target}: native Wayland, clipboard, save/reopen and actual audio PASS')
    except Exception as exc:
        attempt['errors'].append(f'{type(exc).__name__}: {exc}')
        (directory / 'failure-traceback.txt').write_text(traceback.format_exc())
        log(f'{target}: FAIL at {attempt["stage"]}: {exc}')
    finally:
        service.stop(env_id)
        if process is not None:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=5)
        if owner is not None and owner.poll() is None:
            owner.terminate(); owner.wait(timeout=5)
        attempt['finished_at'] = now()
        save(directory / 'result.json', attempt)
        save(run_dir / 'manifest.json', manifest)
    return attempt


def run_native(run_dir, targets):
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    need(manifest.get('preparation_passed'), 'native preparation is incomplete')
    with ClipboardGuard() as guard:
        for target in targets or PACKAGES:
            run_native_target(run_dir, manifest, target)
    manifest['clipboard_restored'] = guard.restored
    manifest['native_finished_at'] = now()
    manifest['native_passed'] = all(e.get('native_attempts', [{}])[-1].get('passed') for e in manifest['targets'].values())
    manifest['host_after_native'] = HostObserver().snapshot()
    save(run_dir / 'manifest.json', manifest)
    return 0 if manifest['native_passed'] else 1


def prepare_native(run_dir):
    manifest = prepare(run_dir)
    manifest["scope"] = (
        "Native GTK/Wayland windows, host/environment clipboard exchange, "
        "real playback streams on the current host audio output, and a local "
        "Tencent Meeting deb in an independent Debian environment. No meeting/login acceptance."
    )
    manifest["package_sources"] = {}
    manifest["preparation_driver_sha256"] = sha(__file__)
    shutil.copyfile(__file__, run_dir / "preparation-driver.py")
    service = service_for(run_dir)
    for target, packages in PACKAGES.items():
        env_id = "native-" + target
        directory = run_dir / target
        directory.mkdir(exist_ok=True)
        docs = run_dir / "documents" / target
        docs.mkdir(parents=True, exist_ok=True)
        record = manifest["targets"].setdefault(target, {})
        record.update(environment_id=env_id, packages=packages, preparation_started_at=now())
        save(run_dir / "manifest.json", manifest)
        try:
            if not service.store.exists(env_id):
                service.create(target, env_id, [str(docs)], "pasta", desktop=True)
            log(f"{target}: installing native Wayland editor and media/clipboard tools")
            txn = service.install_packages(env_id, packages, use_argv=["mousepad", "--version"])
            save(directory / "install-transaction.json", txn)
            record["install_transaction"] = txn["id"]
            need(txn["phase"] == "completed", f"install failed: {txn.get('native_error')} {txn.get('verification')}")
            shutil.copyfile(service.store.inventory_path(env_id), directory / "packages.txt")
            record["package_inventory_sha256"] = sha(directory / "packages.txt")
            record["prepared"] = True
            save(directory / "environment-prepared.json", service.inspect(env_id))
            log(f"{target}: preparation passed")
        except Exception as exc:
            record["prepared"] = False
            record["preparation_error"] = f"{type(exc).__name__}: {exc}"
            log(f"{target}: preparation failed: {exc}")
        finally:
            if service.store.exists(env_id):
                service.stop(env_id)
            record["preparation_finished_at"] = now()
            save(run_dir / "manifest.json", manifest)
    manifest["preparation_passed"] = all(x.get("prepared") for x in manifest["targets"].values())
    save(run_dir / "manifest.json", manifest)
    return 0 if manifest["preparation_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--run-dir", type=Path)
    n = commands.add_parser('native')
    n.add_argument('--run-dir', type=Path, required=True)
    n.add_argument('--target', choices=tuple(PACKAGES), action='append')
    args = parser.parse_args()
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%dT%H%M%S%z")
    run_dir = (args.run_dir or ROOT / "evidence" / ("wayland-media-" + stamp)).resolve()
    log(f"native acceptance directory: {run_dir}")
    if args.command == 'native':
        return run_native(run_dir, args.target)
    return prepare_native(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
