"""Private bus protocol acceptance: no desktop, account, or real container."""
from concurrent.futures import Future
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from gi.repository import Gio, GLib
    from unifier.portal_file_bridge import (
        CHOOSER, PORTAL, REQUEST, REQUEST_PREFIX, REQUEST_TIMEOUT, Client,
        connect, queued_message_size, stamp,
    )
except ImportError:
    Gio = None


class FakeConnection:
    def __init__(self):
        self.serial = 1
        self.sent = []

    def get_unique_name(self):
        return ':1.9'

    def send_message(self, message, flags):
        if not flags & Gio.DBusSendMessageFlags.PRESERVE_SERIAL:
            self.serial += 1
            message.set_serial(self.serial)
        self.sent.append(message)
        return True, message.get_serial()


@unittest.skipUnless(Gio, 'requires host GIO message types')
class PortalBridgeState(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.owner = ':1.3'
        self.relay = SimpleNamespace(
            busy=0, clients=set(), clock=lambda: self.now, emit=Mock(),
            executor=Mock(), portal_owner=Mock(return_value=self.owner),
            store=Mock(), queue_lock=threading.Lock(), queue_limit=1024,
            queued_bytes=0,
        )
        self.idles = []
        idle = patch.object(GLib, 'idle_add', side_effect=self.enqueue)
        idle.start()
        self.addCleanup(idle.stop)
        self.client = self.make_client()

    def enqueue(self, callback, *args, **kwargs):
        self.idles.append((callback, args))
        return len(self.idles)

    def make_client(self):
        client = Client.__new__(Client)
        client.relay = self.relay
        client.up, client.down = FakeConnection(), FakeConnection()
        client.closed = client.close_scheduled = False
        client.hello = True
        client.requests, client.paths, client.upstream_calls, client.pending = {}, {}, {}, {}
        client.retired, client.expired_paths, client.filters = [], set(), []
        self.relay.clients.add(client)
        return client

    def chooser(self, token='fixture'):
        message = Gio.DBusMessage.new_method_call(
            PORTAL, '/org/freedesktop/portal/desktop', CHOOSER, 'OpenFile')
        message.set_serial(110)
        options = {} if token is None else {'handle_token': GLib.Variant('s', token)}
        message.set_body(GLib.Variant('(ssa{sv})', ('', 'fixture', options)))
        self.client.outgoing(message)
        return self.client.up.sent[-1]

    def handle(self, call, path):
        reply = Gio.DBusMessage.new_method_reply(call)
        reply.set_sender(self.owner)
        reply.set_serial(80)
        reply.set_body(GLib.Variant('(o)', (path,)))
        self.client.incoming(reply)

    def response_message(self, path, status=0):
        signal = Gio.DBusMessage.new_signal(path, REQUEST, 'Response')
        signal.set_sender(self.owner)
        signal.set_serial(81)
        signal.set_body(GLib.Variant('(ua{sv})', (status, {})))
        return signal

    def response(self, path, status=0):
        self.client.incoming(self.response_message(path, status))

    def test_timeout_after_handle_retires_signal_and_revokes_completed_copy(self):
        call = self.chooser()
        path = REQUEST_PREFIX + '1_9/fixture'
        self.handle(call, path)
        future = Future()
        self.relay.executor.submit.return_value = future
        self.response(path)
        self.now = REQUEST_TIMEOUT
        self.client.expire()
        self.client.expire()
        self.response(path)
        close = self.client.up.sent[-1]
        self.assertEqual(len(self.client.up.sent), 2)
        self.assertEqual((close.get_destination(), close.get_path(), close.get_member()),
                         (self.owner, path, 'Close'))
        self.assertTrue(close.get_flags() & Gio.DBusMessageFlags.NO_REPLY_EXPECTED)
        self.assertNotEqual(close.get_serial(), call.get_serial())
        self.assertEqual(len(self.client.down.sent), 2)
        self.assertEqual(self.client.down.sent[0].get_reply_serial(), 110)
        self.assertEqual(self.client.down.sent[1].get_body().unpack(), (2, {}))
        result = ['file:///guest/snapshot']
        future.set_result(result)
        callback, args = self.idles.pop()
        callback(*args)
        self.relay.store.revoke.assert_called_once_with(result)
        self.assertEqual(self.relay.busy, 0)
        self.assertEqual(len(self.client.down.sent), 2)

    def test_timeout_before_handle_sends_only_method_error_and_closes_late_handle(self):
        call = self.chooser(token=None)
        self.now = REQUEST_TIMEOUT
        self.client.expire()
        path = REQUEST_PREFIX + '1_9/late'
        self.response(path)
        self.handle(call, path)
        self.response(path)
        self.assertEqual(len(self.client.down.sent), 1)
        error = self.client.down.sent[0]
        self.assertEqual(error.get_message_type(), Gio.DBusMessageType.ERROR)
        self.assertEqual(error.get_reply_serial(), 110)
        self.assertEqual([m.get_member() for m in self.client.up.sent], ['OpenFile', 'Close'])
        self.assertEqual(self.client.up.sent[-1].get_path(), path)
        self.assertFalse(self.client.upstream_calls)
        self.chooser(token='late')
        self.assertEqual(self.client.down.sent[-1].get_message_type(), Gio.DBusMessageType.ERROR)
        self.assertEqual(len(self.client.up.sent), 2)

    def test_early_response_expiration_releases_bytes_without_signal(self):
        call = self.chooser()
        path = REQUEST_PREFIX + '1_9/fixture'
        self.response(path, 1)
        self.assertGreater(self.relay.queued_bytes, 0)
        self.assertFalse(self.client.down.sent)
        self.now = REQUEST_TIMEOUT
        self.client.expire()
        self.handle(call, path)
        self.assertEqual(self.relay.queued_bytes, 0)
        self.assertEqual(len(self.client.down.sent), 1)
        self.assertEqual(self.client.down.sent[0].get_message_type(), Gio.DBusMessageType.ERROR)
        self.assertEqual([m.get_member() for m in self.client.up.sent], ['OpenFile', 'Close'])

    def test_early_responses_share_queue_budget(self):
        self.chooser('first')
        self.chooser('other')
        response = self.response_message(REQUEST_PREFIX + '1_9/first', 1)
        size = queued_message_size(response, response.get_body().get_size())
        self.relay.queue_limit = size
        self.client.incoming(response)
        self.response(REQUEST_PREFIX + '1_9/other', 1)
        self.assertEqual(self.relay.queued_bytes, size)
        self.assertEqual(len(self.idles), 1)
        callback, args = self.idles.pop()
        callback(*args)
        self.assertEqual(self.relay.queued_bytes, 0)
        self.assertFalse(self.client.requests)

    def test_non_chooser_reply_translation_and_internal_reply_filtering(self):
        for guest_serial, error in ((250, False), (251, True)):
            message = Gio.DBusMessage.new_method_call(
                PORTAL, '/org/freedesktop/portal/desktop', 'org.freedesktop.DBus.Properties', 'Get')
            message.set_serial(guest_serial)
            self.client.outgoing(message)
            call = self.client.up.sent[-1]
            if error:
                reply = Gio.DBusMessage.new_method_error_literal(call, 'org.freedesktop.DBus.Error.Failed', 'fixture')
            else:
                reply = Gio.DBusMessage.new_method_reply(call)
            reply.set_serial(82)
            self.client.incoming(reply)
            self.assertEqual(self.client.down.sent[-1].get_reply_serial(), guest_serial)
            self.client.incoming(reply)
        self.assertEqual(len(self.client.down.sent), 2)
        self.assertFalse(self.client.upstream_calls)

    def test_queue_aggregate_budget_releases_on_dispatch_and_close(self):
        first, second = self.client, self.make_client()
        message = Gio.DBusMessage.new_signal('/fixture', 'example.Fixture', 'Signal')
        message.set_body(GLib.Variant('(s)', ('fixture',)))
        size = queued_message_size(message, message.get_body().get_size())
        self.assertGreater(size, message.get_body().get_size())
        self.relay.queue_limit = size * 2
        first.filtered(None, message, True, True)
        second.filtered(None, message, True, True)
        self.assertEqual(self.relay.queued_bytes, size * 2)
        for _ in range(3):
            second.filtered(None, message, True, True)
        self.assertEqual(len(self.idles), 3)
        callback, args = self.idles[2]
        callback(*args)
        self.assertEqual(self.relay.queued_bytes, size)
        for callback, args in self.idles[:2]:
            callback(*args)
        self.assertEqual(self.relay.queued_bytes, 0)
        self.assertEqual(len(first.down.sent), 1)
        self.assertFalse(second.down.sent)
        second.filtered(None, message, True, True)
        self.assertEqual(len(self.idles), 3)

    def test_queue_dispatch_error_releases_all_client_bytes(self):
        message = Gio.DBusMessage.new_signal('/fixture', 'example.Fixture', 'Signal')
        self.client.incoming = Mock(side_effect=ValueError('fixture'))
        for _ in range(2):
            self.client.filtered(None, message, True, True)
        for callback, args in self.idles:
            callback(*args)
        self.assertTrue(self.client.closed)
        self.assertEqual(self.relay.queued_bytes, 0)

    def test_queue_idle_registration_failure_releases_bytes(self):
        message = Gio.DBusMessage.new_signal('/fixture', 'example.Fixture', 'Signal')
        with patch.object(GLib, 'idle_add', side_effect=[RuntimeError('fixture'), 1]) as idle:
            self.client.filtered(None, message, True, True)
            self.assertEqual(idle.call_count, 2)
        self.assertFalse(self.client.pending)
        self.assertEqual(self.relay.queued_bytes, 0)


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
        self.close_senders = []
        self.closed_request = threading.Event()
        self.client = None

    def start_relay(self, *, request_timeout=None):
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
        env['G_DEBUG'] = 'fatal-criticals'
        env['UNIFIER_FIXTURE_REQUEST_TIMEOUT'] = str(
            REQUEST_TIMEOUT if request_timeout is None else request_timeout)
        for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS'):
            env.pop(name, None)
        self.relay = subprocess.Popen(['/usr/bin/python3', '-c',
            'from unittest.mock import patch; from collections import namedtuple; '
            'import os; import unifier.portal_file_bridge as bridge; '
            'bridge.REQUEST_TIMEOUT = float(os.environ["UNIFIER_FIXTURE_REQUEST_TIMEOUT"]); '
            'patch("unifier.portal_delivery.shutil.disk_usage", return_value=namedtuple("Disk", "total used free")(100*1024**3, 0, 90*1024**3)).start(); bridge.main()',
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
                self.close_senders.append(sender)
                self.closed_request.set()
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

    def select(self, *, options=None, method='OpenFile', close=False, response_timeout=5):
        if self.client is None:
            self.start_relay()
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
        self.assertTrue(completed.wait(response_timeout), (self.root / 'audit').read_text())
        return results

    def test_success_probe_translation_read_and_cleanup(self):
        self.start_relay()
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

    def test_screencast_pipewire_fd_crosses_file_delivery_relay(self):
        interface = 'org.freedesktop.portal.ScreenCast'
        path = '/org/freedesktop/portal/desktop'
        node = Gio.DBusNodeInfo.new_for_xml(f'''<node><interface name="{interface}">
          <method name="OpenPipeWireRemote"><arg type="o" direction="in"/>
          <arg type="a{{sv}}" direction="in"/><arg type="h" direction="out"/></method>
        </interface></node>''')
        payload = b'synthetic PipeWire descriptor'

        def open_remote(_connection, _sender, _path, _interface, _method, _params, call):
            with tempfile.TemporaryFile() as source:
                source.write(payload)
                source.seek(0)
                fds = Gio.UnixFDList.new()
                handle = fds.append(source.fileno())
                call.return_value_with_unix_fd_list(GLib.Variant('(h)', (handle,)), fds)

        registration = self.portal.register_object(path, node.interfaces[0], open_remote, None, None)
        self.addCleanup(self.portal.unregister_object, registration)
        self.start_relay()
        result, fds = self.client.call_with_unix_fd_list_sync(
            PORTAL, path, interface, 'OpenPipeWireRemote',
            GLib.Variant('(oa{sv})', (path + '/session/fixture', {})),
            GLib.VariantType.new('(h)'), Gio.DBusCallFlags.NONE, 2000, None, None)
        self.assertEqual(fds.get_length(), 1)
        with os.fdopen(fds.get(result.unpack()[0]), 'rb') as source:
            self.assertEqual(source.read(), payload)

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

    def test_timeout_close_crosses_proxy_as_original_request_owner(self):
        self.defer_response = True
        self.start_relay(request_timeout=0.1)
        self.assertEqual(self.select(response_timeout=3), [(2, {})])
        self.assertTrue(self.closed_request.wait(3), (self.root / 'audit').read_text())
        self.assertEqual(self.close_senders, [self.calls[0][0]])
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
