"""Private bus protocol acceptance: no desktop, account, or real container."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest

try:
    from gi.repository import Gio, GLib
    from unifier.portal_file_bridge import CHOOSER, PORTAL, REQUEST, connect, stamp
except ImportError:
    Gio = None


@unittest.skipUnless(Gio and shutil.which('dbus-daemon'), 'requires host GIO and private D-Bus')
class PortalBridgeProtocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.daemon = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop, self.daemon)
        self.address = self.daemon.stdout.readline().strip()
        self.addCleanup(self.daemon.stdout.close)
        self.portal = connect(self.address)
        self.addCleanup(self.portal.close_sync, None)
        self.portal.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'RequestName', GLib.Variant('(su)', (PORTAL, 0)), None, Gio.DBusCallFlags.NONE, 2000, None)
        xml = '''<node><interface name="org.freedesktop.portal.FileChooser">
          <method name="OpenFile"><arg type="s" direction="in"/><arg type="s" direction="in"/>
          <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
          <method name="SaveFile"><arg type="s" direction="in"/><arg type="s" direction="in"/>
          <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
          <property name="version" type="u" access="read"/>
        </interface></node>'''
        info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
        self.registration = self.portal.register_object('/org/freedesktop/portal/desktop', info,
            self.called, lambda *_: GLib.Variant('u', 3), None)
        self.addCleanup(self.portal.unregister_object, self.registration)
        self.loop = GLib.MainLoop()
        self.thread = threading.Thread(target=self.loop.run)
        self.thread.start()
        self.addCleanup(self.end_loop)
        self.source = self.root / '实验二 (copy) %.doc'
        self.source.write_bytes(b'test attachment\x00\xff' * 31)
        self.uris = [self.source.as_uri()]
        self.status = 0
        self.early = False
        self.spoof = False
        self.defer_response = False
        self.calls = []
        self.audit = (self.root / 'audit').open('wb')
        self.addCleanup(self.audit.close)
        # Relay connects to a private bus. The product connects to the existing
        # filtered proxy; no user session address is inherited here.
        self.proxy = subprocess.Popen(['xdg-dbus-proxy', self.address, 'bus', '--filter',
            '--talk=' + PORTAL, '--talk=org.freedesktop.Notifications'], cwd=self.root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, self.proxy)
        until = time.monotonic() + 3
        while not (self.root / 'bus').exists() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue((self.root / 'bus').is_socket())
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS'):
            env.pop(name, None)
        self.relay = subprocess.Popen(['/usr/bin/python3', '-c',
            'from unittest.mock import patch; from collections import namedtuple; '
            'from unifier.portal_file_bridge import main; '
            'patch("unifier.portal_delivery.shutil.disk_usage", return_value=namedtuple("Disk", "total used free")(100*1024**3, 0, 90*1024**3)).start(); main()',
            '--guest', '/run/multilinux-portal', '--shares', '[]', '--parent', str(os.getpid()),
            '--stamp', str(stamp(os.getpid()))], cwd=self.root, env=env, stdout=self.audit, stderr=self.audit)
        self.addCleanup(self.stop, self.relay)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if b'"event": "ready"' in (self.root / 'audit').read_bytes():
                break
            if self.relay.poll() is not None:
                self.fail((self.root / 'audit').read_text())
            time.sleep(.02)
        else:
            self.fail('relay readiness timeout')
        self.client = connect('unix:path=' + str(self.root / 'files-bus'))
        self.addCleanup(self.close_connection, self.client)

    @staticmethod
    def close_connection(connection):
        if not connection.is_closed():
            connection.close_sync(None)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def end_loop(self):
        self.loop.quit()
        self.thread.join(2)

    def called(self, connection, sender, path, interface, method, params, invocation):
        self.calls.append((sender, method))
        options = params.unpack()[2]
        request = '/org/freedesktop/portal/desktop/request/' + sender[1:].replace('.', '_') + '/' + options['handle_token']
        payload = GLib.Variant('(ua{sv})', (self.status, {'uris': GLib.Variant('as', self.uris)}))
        if self.defer_response:
            info = Gio.DBusNodeInfo.new_for_xml('<node><interface name="org.freedesktop.portal.Request"><method name="Close"/></interface></node>').interfaces[0]
            def close_called(c, sender, p, interface, method, args, call):
                call.return_value(None)
                connection.emit_signal(sender, request, REQUEST, 'Response', payload)
            registration = connection.register_object(request, info, close_called, None, None)
            self.addCleanup(connection.unregister_object, registration)
        if self.spoof:
            stranger = connect(self.address)
            stranger.emit_signal(sender, request, REQUEST, 'Response', payload)
            stranger.flush_sync(None)
            stranger.close_sync(None)
        if self.early:
            connection.emit_signal(sender, request, REQUEST, 'Response', payload)
            connection.flush_sync(None)
        invocation.return_value(GLib.Variant('(o)', (request,)))
        if not self.early and not self.defer_response:
            connection.emit_signal(sender, request, REQUEST, 'Response', payload)

    def select(self, *, options=None, method='OpenFile', close=False):
        completed = threading.Event()
        results = []
        sub = self.client.signal_subscribe(None, REQUEST, 'Response', None, None,
            Gio.DBusSignalFlags.NONE, lambda c, s, p, i, m, body: (results.append(body.unpack()), completed.set()))
        self.addCleanup(self.client.signal_unsubscribe, sub)
        args = {'handle_token': GLib.Variant('s', 'test' + str(len(self.calls)))}
        args.update(options or {})
        reply = self.client.call_sync(PORTAL, '/org/freedesktop/portal/desktop', CHOOSER, method,
            GLib.Variant('(ssa{sv})', ('', 'fixture', args)), GLib.VariantType.new('(o)'),
            Gio.DBusCallFlags.NONE, 5000, None)
        self.assertTrue(reply.unpack()[0].startswith('/org/freedesktop/portal/desktop/request/'))
        if close:
            self.client.call_sync(PORTAL, reply.unpack()[0], REQUEST, 'Close', None, None,
                                  Gio.DBusCallFlags.NONE, 2000, None)
        self.assertTrue(completed.wait(5), (self.root / 'audit').read_text())
        return results

    def test_success_probe_translation_read_and_cleanup(self):
        version = self.client.call_sync(PORTAL, '/org/freedesktop/portal/desktop',
            'org.freedesktop.DBus.Properties', 'Get', GLib.Variant('(ss)', (CHOOSER, 'version')),
            None, Gio.DBusCallFlags.NONE, 2000, None)
        self.assertEqual(version.unpack()[0], 3)
        result = self.select()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 0, (self.root / 'audit').read_text())
        from unifier.portal_delivery import local_path
        guest = local_path(result[0][1]['uris'][0])
        host = self.root / guest.relative_to('/run/multilinux-portal')
        self.assertEqual(host.name, self.source.name)
        self.assertEqual(host.read_bytes(), self.source.read_bytes())
        until = time.monotonic() + 2
        while b'selected-file-read' not in (self.root / 'audit').read_bytes() and time.monotonic() < until:
            time.sleep(.01)
        self.assertIn('selected-file-read', (self.root / 'audit').read_text())
        self.stop(self.relay)
        self.assertFalse(host.exists())
        self.assertFalse(list(self.root.glob('selected-*')))
        self.assertEqual(self.relay.returncode, 0, (self.root / 'audit').read_text())

    def test_early_response_and_multiple_files(self):
        self.early = True
        second = self.root / 'second.txt'
        second.write_text('two')
        self.uris.append(second.as_uri())
        result = self.select(options={'multiple': GLib.Variant('b', True)})
        self.assertEqual(result[0][0], 0, (self.root / 'audit').read_text())
        self.assertEqual(len(result[0][1]['uris']), 2)

    def test_cancel_does_not_copy(self):
        self.status = 1
        self.assertEqual(self.select()[0][0], 1)
        self.assertFalse(list(self.root.glob('selected-*/*')))

    def test_close_rejects_a_late_success(self):
        self.defer_response = True
        self.assertEqual(self.select(close=True)[0][0], 1)
        self.assertFalse(list(self.root.glob('selected-*/*')))

    def test_write_selection_is_explicit_failure(self):
        self.assertEqual(self.select(options={'writable': GLib.Variant('b', True)})[0], (2, {}))
        self.assertFalse(list(self.root.glob('selected-*/*')))

    def test_spoofed_response_cannot_authorize_copy(self):
        self.spoof = True
        result = self.select()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 0, (self.root / 'audit').read_text())
        self.assertEqual(len(list(self.root.glob('selected-*/*'))), 1)

    def test_nonlocal_selection_fails_without_host_uri(self):
        self.uris = ['https://example.invalid/file']
        self.assertEqual(self.select()[0], (2, {}))


if __name__ == '__main__':
    unittest.main()
