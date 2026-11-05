"""On-demand ownership, package routing and menu-only registration contracts."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from unifier.errors import UnifierError
from unifier.on_demand import LaunchGroup, live_leases, managed_open, process_stamp
from unifier.routing import route_package
from unifier.runtime import CreateSpec, creation_argv
from test_desktop import desktop_service, SESSION


def ready(root):
    service, runtime = desktop_service(root)
    service.create('debian13', None, [], desktop=True, session=SESSION)
    service.add_app('debian13', 'notes', '/usr/bin/true', ['/usr/bin/true'], on_demand=True)
    return service, runtime


class ManagedLifetime(unittest.TestCase):
    def test_last_lease_stops_only_the_environment_started_by_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            a, b = LaunchGroup(service, 'debian13'), LaunchGroup(service, 'debian13')
            first = a.acquire('notes'); second = b.acquire('notes')
            a.release(first)
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')
            b.release(second)
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')

    def test_manually_running_environment_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            service.start('debian13')
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')

    def test_exception_releases_lease_and_stops_started_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            def fail(*_args, **_kwargs):
                error = RuntimeError('launch failed')
                error.execution_started = False
                raise error
            service.start_app_execution = fail
            with self.assertRaisesRegex(RuntimeError, 'launch failed'):
                managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')
            self.assertEqual(LaunchGroup(service, 'debian13').read()['leases'], {})

    def test_pid_reuse_does_not_keep_an_old_lease_alive(self):
        stamp = process_stamp(os.getpid())
        items = {'old': {'pid': os.getpid(), 'stamp': stamp + 1},
                 'current': {'pid': os.getpid(), 'stamp': stamp}}
        self.assertEqual(set(live_leases(items)), {'current'})


class DesktopAndRouting(unittest.TestCase):
    def test_menu_registration_has_icon_identity_and_preserves_mime(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            mime = service.host_desktop.mimeapps
            mime.parent.mkdir(parents=True)
            original = b'[Default Applications]\ntext/plain=kate.desktop;\n'
            mime.write_bytes(original)
            launch = service.export_app('notes', menu_only=True, display_name='腾讯会议',
                                        icon='/safe/icon.svg', wm_class='wemeetapp', desktop_alias='wemeetapp')
            path = Path(launch['export']['desktop_path']);text = path.read_text()
            self.assertIn('Name=腾讯会议', text);self.assertIn('StartupWMClass=wemeetapp', text)
            self.assertIn('Icon=/safe/icon.svg', text);self.assertNotIn('MimeType=', text)
            self.assertEqual(mime.read_bytes(), original)
            service.unexport_app('notes')
            self.assertFalse(path.exists());self.assertEqual(mime.read_bytes(), original)

    def test_menu_export_does_not_replace_foreign_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            path = service.host_desktop.applications / 'wemeetapp.desktop'
            path.parent.mkdir(parents=True);path.write_text('foreign application\n')
            with self.assertRaisesRegex(UnifierError, 'refusing to replace'):
                service.export_app('notes', menu_only=True, desktop_alias='wemeetapp')
            self.assertEqual(path.read_text(), 'foreign application\n')

    def test_supported_formats_route_without_starting_runtime_and_reject_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, header, expected in [('test.deb', b'!<arch>\n', 'debian13'),
                                            ('test.rpm', b'\xed\xab\xee\xdb', 'fedora44'),
                                            ('test.pkg.tar.zst', b'\x28\xb5\x2f\xfd', 'arch')]:
                path = root / name;path.write_bytes(header)
                result = route_package(path)
                self.assertEqual(result['target'], expected)
                self.assertEqual(result['validation'], 'native-package-manager-required')
            bad = root / 'bad.deb';bad.write_bytes(b'not a package')
            with self.assertRaisesRegex(UnifierError, 'mismatch'):
                route_package(bad)

    def test_camera_is_explicit_and_host_session_directory_is_not_mounted(self):
        spec = CreateSpec('name', 'id', '/project', 'repo@sha256:' + 'a'*64, (), devices=('/dev/video0',))
        command = creation_argv(spec)
        self.assertIn('/dev/video0:/dev/video0:rw', command)
        with self.assertRaises(UnifierError):
            creation_argv(replace(spec, devices=('/dev/sda',)))
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = desktop_service(Path(temporary))
            record = service.create('debian13', None, [], desktop=True, session=SESSION, portals=True)
            self.assertIn('portal_proxy', record['desktop'])
            self.assertNotIn('/run/user/1000', [m['host'] for m in record['desktop']['mounts']])
            self.assertNotIn('/tmp/ml-xauthority', [m['container'] for m in record['desktop']['mounts']])

    def test_gpu_and_ipc_host_support(self):
        spec = CreateSpec('name', 'id', '/project', 'repo@sha256:' + 'a'*64, (),
                          devices=('/dev/video0', '/dev/dri/renderD128'), ipc='host')
        command = creation_argv(spec)
        self.assertIn('--ipc=host', command)
        self.assertIn('/dev/dri/renderD128:/dev/dri/renderD128:rw', command)
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = desktop_service(Path(temporary))
            with self.assertRaisesRegex(UnifierError, 'require --desktop'):
                service.create('debian13', None, [], desktop=False, gpu=True)
            record = service.create('debian13', None, [], desktop=True, session=SESSION, gpu=True)
            self.assertEqual(record['runtime']['ipc'], 'host')


if __name__ == '__main__':
    unittest.main()
