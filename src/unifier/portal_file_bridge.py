"""Lease-owned D-Bus relay for native toolkit Portal file delivery.

The existing xdg-dbus-proxy remains the bus allowlist. Each authenticated peer
gets its own upstream bus connection/unique name. Only a correlated successful
Response from the current host Portal owner authorizes selected-file delivery.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import ctypes
import struct
import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

from gi.repository import Gio, GLib

from unifier.portal_delivery import SelectionStore

PORTAL = 'org.freedesktop.portal.Desktop'
CHOOSER = 'org.freedesktop.portal.FileChooser'
REQUEST = 'org.freedesktop.portal.Request'
BUS = 'org.freedesktop.DBus'
PRESERVE = Gio.DBusSendMessageFlags.PRESERVE_SERIAL


def variants(value):
    return {value.get_child_value(i).get_child_value(0).get_string():
            value.get_child_value(i).get_child_value(1).get_variant()
            for i in range(value.n_children())}


def connect(address):
    return Gio.DBusConnection.new_for_address_sync(address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)


def stamp(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else int(fields[19])
    except (OSError, ValueError):
        return None


class Client:
    def __init__(self, relay, downstream):
        self.relay = relay
        self.down = downstream
        self.up = connect(relay.upstream)
        self.closed = False
        self.hello = False
        self.requests = {}
        self.paths = {}
        self.retired = deque(maxlen=256)
        self.pending = 0
        self.lock = threading.Lock()
        self.filters = [(self.down, self.down.add_filter(self.filtered, False)),
                        (self.up, self.up.add_filter(self.filtered, True))]
        self.down.connect('closed', lambda *_: GLib.idle_add(self.close))
        self.up.connect('closed', lambda *_: GLib.idle_add(self.close))
        self.down.start_message_processing()

    def filtered(self, connection, message, incoming, upstream):
        if not incoming:
            return message
        # GIO invokes filters on its I/O thread. Never make synchronous bus
        # calls here; bounded dispatch and disk work run outside that thread.
        with self.lock:
            self.pending += 1
            overflow = self.pending > 256
        body = message.get_body()
        if overflow or (body is not None and body.get_size() > 16*1024**2):
            GLib.idle_add(self.close)
        else:
            GLib.idle_add(self.dispatch, message, upstream)
        return None

    def send(self, destination, message):
        if not self.closed:
            destination.send_message(message, PRESERVE)

    def fail(self, call, reason):
        reply = Gio.DBusMessage.new_method_error_literal(call,
                    'org.freedesktop.portal.Error.Failed', reason)
        reply.set_sender(PORTAL)
        self.down.send_message(reply, Gio.DBusSendMessageFlags.NONE)

    def dispatch(self, message, upstream):
        with self.lock:
            self.pending -= 1
        if self.closed:
            return False
        try:
            if upstream:
                self.incoming(message)
            else:
                self.outgoing(message)
        except Exception as exc:
            self.relay.emit(event='relay-error', code=type(exc).__name__)
            self.close()
        return False

    def outgoing(self, message):
        call = message.get_message_type() == Gio.DBusMessageType.METHOD_CALL
        if call and message.get_interface() == BUS and message.get_member() == 'Hello':
            if self.hello:
                self.fail(message, 'Hello already completed')
                return
            self.hello = True
            reply = Gio.DBusMessage.new_method_reply(message)
            reply.set_sender(BUS)
            reply.set_body(GLib.Variant('(s)', (self.up.get_unique_name(),)))
            self.down.send_message(reply, Gio.DBusSendMessageFlags.NONE)
            return
        if not self.hello:
            self.close()
            return
        if call and message.get_interface() == REQUEST and message.get_member() == 'Close':
            request = self.paths.get(message.get_path())
            if request:
                request['cancelled'].set()
        if (call and message.get_destination() == PORTAL and message.get_interface() == CHOOSER
                and message.get_member() in ('OpenFile', 'SaveFile', 'SaveFiles')):
            if len(self.requests) >= 16 or self.relay.busy >= 16:
                self.fail(message, 'too many pending file selections')
                return
            body = message.get_body()
            if body is None or body.get_type_string() != '(ssa{sv})':
                self.fail(message, 'invalid file selection arguments')
                return
            options = body.unpack()[2]
            owner = self.relay.portal_owner(activate=True)
            if owner is None:
                self.fail(message, 'host Portal owner is unavailable')
                return
            request = {'serial': message.get_serial(), 'owner': owner,
                       'method': message.get_member(), 'options': options,
                       'cancelled': threading.Event(), 'started': time.monotonic(),
                       'responding': False, 'paths': set()}
            self.requests[message.get_serial()] = request
            token = options.get('handle_token')
            if isinstance(token, str) and token and all(c.isascii() and (c.isalnum() or c == '_') for c in token):
                path = '/org/freedesktop/portal/desktop/request/' + self.up.get_unique_name()[1:].replace('.', '_') + '/' + token
                self.paths[path] = request
                request['paths'].add(path)
            self.relay.emit(event='file-chooser-call', method=request['method'], client=self.up.get_unique_name())
        forwarded = message.copy()
        forwarded.set_sender(None)
        self.send(self.up, forwarded)

    def forget(self, request):
        self.requests.pop(request['serial'], None)
        for path in request['paths']:
            if self.paths.get(path) is request:
                self.paths.pop(path, None)
            self.retired.append(path)

    def incoming(self, message):
        kind = message.get_message_type()
        if kind in (Gio.DBusMessageType.METHOD_RETURN, Gio.DBusMessageType.ERROR):
            request = self.requests.get(message.get_reply_serial())
            if request:
                if kind == Gio.DBusMessageType.ERROR:
                    request['cancelled'].set()
                    self.forget(request)
                elif message.get_sender() != request['owner']:
                    self.close()
                    return
                else:
                    body = message.get_body()
                    if body is None or body.get_type_string() != '(o)':
                        self.close()
                        return
                    path = body.unpack()[0]
                    if not path.startswith('/org/freedesktop/portal/desktop/request/'):
                        self.close()
                        return
                    self.paths[path] = request
                    request['paths'].add(path)
        if (kind == Gio.DBusMessageType.SIGNAL and message.get_interface() == REQUEST
                and message.get_member() == 'Response'):
            request = self.paths.get(message.get_path())
            if request is None and message.get_path() in self.retired:
                return
            if request:
                # Identity is verified on both sides of the asynchronous call.
                if message.get_sender() != request['owner'] or request['responding']:
                    return
                if self.relay.portal_owner() != request['owner']:
                    request['cancelled'].set()
                request['responding'] = True
                body = message.get_body()
                if body is None or body.get_type_string() != '(ua{sv})':
                    self.close()
                    return
                status, values = body.unpack()
                if status != 0 or request['cancelled'].is_set():
                    self.forget(request)
                    if request['cancelled'].is_set() and status == 0:
                        failed = message.copy()
                        failed.set_body(GLib.Variant('(ua{sv})', (1, {})))
                        self.send(self.down, failed)
                    else:
                        self.send(self.down, message)
                    return
                if self.relay.busy >= 16:
                    self.finish(request, message, None, 'request-budget')
                    return
                self.relay.busy += 1
                future = self.relay.executor.submit(self.deliver, request, values.get('uris'))
                future.add_done_callback(lambda done: GLib.idle_add(self.completed, request, message, done))
                return
        self.send(self.down, message)

    def deliver(self, request, uris):
        options = request['options']
        return self.relay.store.deliver(uris, method=request['method'],
            multiple=options.get('multiple', False) or request['method'] == 'SaveFiles',
            directory=options.get('directory', False), writable=options.get('writable', False),
            cancelled=request['cancelled'])

    def completed(self, request, message, future):
        self.relay.busy -= 1
        try:
            result = future.result()
            error = None
        except Exception as exc:
            result, error = None, type(exc).__name__
        self.finish(request, message, result, error)
        return False

    def finish(self, request, message, result, error):
        self.forget(request)
        if self.closed or request['cancelled'].is_set():
            if result:
                self.relay.store.revoke(result)
            return
        forwarded = message.copy()
        if error:
            forwarded.set_body(GLib.Variant('(ua{sv})', (2, {})))
            self.relay.emit(event='selection-refused', code=error)
        else:
            values = variants(message.get_body().get_child_value(1))
            values['uris'] = GLib.Variant('as', result)
            forwarded.set_body(GLib.Variant('(ua{sv})', (0, values)))
            self.relay.emit(event='selection-delivered', request=message.get_path(),
                            portal_sender=message.get_sender(), client=self.up.get_unique_name(), uris=result)
        self.send(self.down, forwarded)

    def expire(self):
        for request in list(self.requests.values()):
            if time.monotonic() - request['started'] > 300:
                request['cancelled'].set()
                self.forget(request)

    def close(self):
        if self.closed:
            return False
        self.closed = True
        for request in self.requests.values():
            request['cancelled'].set()
        self.requests.clear()
        self.paths.clear()
        for connection, identity in self.filters:
            connection.remove_filter(identity)
            if not connection.is_closed():
                connection.close(None, None, None)
        self.relay.clients.discard(self)
        return False


class Relay:
    def __init__(self, directory, guest, shares, *, upstream='unix:path=bus', parent=None):
        self.directory = Path(directory)
        self.upstream = upstream
        self.parent = parent
        self.clients = set()
        self.audit_bytes = 0
        self.watches = {}
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.read_fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.read_fd < 0:
            raise OSError(ctypes.get_errno(), 'cannot monitor selected-file reads')
        GLib.io_add_watch(self.read_fd, GLib.IO_IN, self.read_events)
        self.busy = 0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='portal-selection')
        self.control = connect(upstream)
        self.loop = GLib.MainLoop()
        observer = Gio.DBusAuthObserver()
        observer.connect('authorize-authenticated-peer', self.authorize)
        self.server = Gio.DBusServer.new_sync('unix:path=files-bus', Gio.DBusServerFlags.NONE,
                                             Gio.dbus_generate_guid(), observer, None)
        self.server.connect('new-connection', self.accept)
        self.store = SelectionStore(directory, guest, shares, emit=self.emit)

    @staticmethod
    def authorize(observer, stream, credentials):
        return credentials is not None and credentials.get_unix_user() == os.getuid()

    def emit(self, **values):
        # stdout is an owner-created append-only audit descriptor, not a path
        # reopened from the guest-writable bridge directory.
        if values.get('event') == 'selection-prepared':
            # Called by the serial copy worker, before returning any URI.
            for entry in values['files']:
                if entry['kind'] == 'snapshot':
                    wd = self.libc.inotify_add_watch(self.read_fd,
                        ctypes.c_char_p(os.fsencode(entry['host_snapshot'])), 0x1)
                    if wd >= 0:
                        self.watches[wd] = entry['guest_path']
        line = json.dumps({'time': time.time(), **values}, ensure_ascii=True)
        if self.audit_bytes + len(line) < 1024*1024:
            self.audit_bytes += len(line) + 1
            print(line, flush=True)

    def read_events(self, fd, condition):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return True
        offset = 0
        while offset + 16 <= len(data):
            wd, mask, cookie, size = struct.unpack_from('iIII', data, offset)
            offset += 16 + size
            if mask & 1:
                path = self.watches.pop(wd, None)
                if path:
                    self.libc.inotify_rm_watch(fd, wd)
                    self.emit(event='selected-file-read', guest_path=path)
        return True

    def portal_owner(self, *, activate=False):
        try:
            return self.control.call_sync(BUS, '/org/freedesktop/DBus', BUS, 'GetNameOwner',
                GLib.Variant('(s)', (PORTAL,)), GLib.VariantType.new('(s)'),
                Gio.DBusCallFlags.NONE, 1500, None).unpack()[0]
        except GLib.Error:
            if activate:
                try:
                    self.control.call_sync(BUS, '/org/freedesktop/DBus', BUS, 'StartServiceByName',
                        GLib.Variant('(su)', (PORTAL, 0)), GLib.VariantType.new('(u)'),
                        Gio.DBusCallFlags.NONE, 1500, None)
                except GLib.Error:
                    return None
                return self.portal_owner()
            return None

    def accept(self, server, connection):
        if len(self.clients) >= 16:
            return False
        try:
            client = Client(self, connection)
        except Exception as exc:
            self.emit(event='client-refused', code=type(exc).__name__)
            return False
        self.clients.add(client)
        return True

    def tick(self):
        if self.parent and stamp(self.parent[0]) != self.parent[1]:
            self.loop.quit()
            return False
        for client in list(self.clients):
            client.expire()
        return True

    def run(self):
        self.server.start()
        self.emit(event='ready', pid=os.getpid(), selection_directory=str(self.store.directory),
                  selection_identity=[self.store.root_identity.st_dev, self.store.root_identity.st_ino])
        GLib.timeout_add(500, self.tick)
        try:
            self.loop.run()
        finally:
            self.server.stop()
            for client in list(self.clients):
                client.close()
            self.store.cancelled.set()
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.store.close()
            os.close(self.read_fd)
            self.control.close_sync(None)
            Path('files-bus').unlink(missing_ok=True)
            self.emit(event='stopped')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--guest', required=True)
    parser.add_argument('--shares', required=True)
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--stamp', type=int, required=True)
    args = parser.parse_args()
    relay = Relay(Path.cwd(), args.guest, json.loads(args.shares), parent=(args.parent, args.stamp))
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (relay.loop.quit(), False)[1])
    relay.run()


if __name__ == '__main__':
    main()
