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

from unifier import __version__
from unifier.portal_delivery import SelectionStore
from unifier.file_access import PROTOCOL_VERSION

PORTAL = 'org.freedesktop.portal.Desktop'
CHOOSER = 'org.freedesktop.portal.FileChooser'
REQUEST = 'org.freedesktop.portal.Request'
BUS = 'org.freedesktop.DBus'
PRESERVE = Gio.DBusSendMessageFlags.PRESERVE_SERIAL
MAX_MESSAGE_BODY = 16 * 1024**2
MAX_QUEUED_BYTES = 32 * 1024**2
REQUEST_TIMEOUT = 300
REQUEST_PREFIX = '/org/freedesktop/portal/desktop/request/'


def queued_message_size(message, body_size):
    # GVariant storage sizes are estimates. Include space for the fixed
    # D-Bus header, header field tags, alignment and variant metadata.
    return 128 + body_size + sum(message.get_header(field).get_size() + 16
                                 for field in message.get_header_fields()
                                 if field)


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
        self.expired_paths = set()
        self.upstream_calls = {}
        self.pending = {}
        self.close_scheduled = False
        self.filters = [(self.down, self.down.add_filter(self.filtered, False)),
                        (self.up, self.up.add_filter(self.filtered, True))]
        self.down.connect('closed', self.schedule_close)
        self.up.connect('closed', self.schedule_close)
        self.down.start_message_processing()

    def filtered(self, connection, message, incoming, upstream):
        if not incoming:
            return message
        # GIO invokes filters on its I/O thread. Never make synchronous bus
        # calls here; bounded dispatch and disk work run outside that thread.
        body = message.get_body()
        body_size = body.get_size() if body is not None else 0
        size = queued_message_size(message, body_size)
        with self.relay.queue_lock:
            if self.closed or self.close_scheduled:
                return None
            if (len(self.pending) >= 256 or body_size > MAX_MESSAGE_BODY
                    or self.relay.queued_bytes + size > self.relay.queue_limit):
                self.schedule_close_locked()
                return None
            ticket = object()
            self.pending[ticket] = (message, upstream, size)
            self.relay.queued_bytes += size
            try:
                GLib.idle_add(self.dispatch, ticket)
            except Exception:
                self.pending.pop(ticket)
                self.relay.queued_bytes -= size
                self.schedule_close_locked()
        return None

    def schedule_close(self, *_):
        with self.relay.queue_lock:
            self.schedule_close_locked()

    def schedule_close_locked(self):
        if not self.closed and not self.close_scheduled:
            self.close_scheduled = True
            GLib.idle_add(self.close, priority=GLib.PRIORITY_DEFAULT)

    def send(self, destination, message):
        if not self.closed:
            destination.send_message(message, PRESERVE)

    @staticmethod
    def error_reply(call, reason):
        reply = Gio.DBusMessage.new_method_error_literal(call,
                    'org.freedesktop.portal.Error.Failed', reason)
        reply.set_sender(PORTAL)
        return reply

    def fail(self, call, reason):
        self.down.send_message(self.error_reply(call, reason), Gio.DBusSendMessageFlags.NONE)

    def dispatch(self, ticket):
        with self.relay.queue_lock:
            queued = self.pending.pop(ticket, None)
            if queued is None:
                return False
            message, upstream, size = queued
            self.relay.queued_bytes -= size
            if self.closed or self.close_scheduled:
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
        request = None
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
            request = None
        if (call and message.get_destination() == PORTAL and message.get_interface() == CHOOSER
                and message.get_member() in ('OpenFile', 'SaveFile', 'SaveFiles')):
            if len(self.requests) >= 16 or self.relay.busy >= 16 or len(self.expired_paths) >= 256:
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
            request = {'timeout_reply': self.error_reply(message, 'file selection timed out'), 'owner': owner,
                       'method': message.get_member(), 'options': options,
                       'cancelled': threading.Event(), 'started': self.relay.clock(),
                       'responding': False, 'paths': set(), 'handle': None,
                       'early_response': None}
            token = options.get('handle_token')
            if isinstance(token, str) and token and all(c.isascii() and (c.isalnum() or c == '_') for c in token):
                path = REQUEST_PREFIX + self.up.get_unique_name()[1:].replace('.', '_') + '/' + token
                if path in self.paths or path in self.retired or path in self.expired_paths:
                    self.fail(message, 'file selection handle is already in use')
                    return
                self.paths[path] = request
                request['paths'].add(path)
            self.relay.emit(event='file-chooser-call', method=request['method'], client=self.up.get_unique_name())
        forwarded = message.copy()
        forwarded.set_sender(None)
        expects_reply = call and not (message.get_flags() & Gio.DBusMessageFlags.NO_REPLY_EXPECTED)
        if expects_reply and len(self.upstream_calls) >= 4096:
            if request:
                self.forget(request)
            self.fail(message, 'too many pending relay calls')
            return
        # Let GIO allocate all outgoing serials on this connection, including
        # timeout Close messages, and translate replies back to guest serials.
        sent, serial = self.up.send_message(forwarded, Gio.DBusSendMessageFlags.NONE)
        if not sent:
            self.close()
            return
        if expects_reply:
            self.upstream_calls[serial] = (message.get_serial(), request)
        if request:
            request['serial'] = serial
            self.requests[serial] = request

    def forget(self, request):
        self.requests.pop(request.get('serial'), None)
        self.take_early_response(request)
        for path in request['paths']:
            if self.paths.get(path) is request:
                self.paths.pop(path, None)
            self.retired.append(path)

    def take_early_response(self, request):
        early = request.pop('early_response', None)
        if early is None:
            return None
        message, size = early
        with self.relay.queue_lock:
            self.relay.queued_bytes -= size
        return message

    @staticmethod
    def request_handle(message):
        body = message.get_body()
        if body is not None and body.get_type_string() == '(o)':
            path = body.unpack()[0]
            if path.startswith(REQUEST_PREFIX):
                return path
        return None

    def close_host_request(self, request, path):
        call = Gio.DBusMessage.new_method_call(request['owner'], path, REQUEST, 'Close')
        call.set_flags(Gio.DBusMessageFlags.NO_REPLY_EXPECTED)
        # No callback or pending reply is needed. GIO queues the send on the
        # connection that originally owned this request.
        try:
            self.up.send_message(call, Gio.DBusSendMessageFlags.NONE)
        except GLib.Error as exc:
            self.relay.emit(event='request-close-failed', code=type(exc).__name__)

    def incoming(self, message):
        kind = message.get_message_type()
        if kind in (Gio.DBusMessageType.METHOD_RETURN, Gio.DBusMessageType.ERROR):
            reply_serial = message.get_reply_serial()
            call = self.upstream_calls.get(reply_serial)
            if call is None:
                return
            guest_serial, request = call
            if request and not request.get('expired'):
                self.expire_request(request, self.relay.clock())
                guest_serial, request = self.upstream_calls[reply_serial]
            self.upstream_calls.pop(reply_serial)
            if request and request.get('expired'):
                path = None
                if kind == Gio.DBusMessageType.METHOD_RETURN and message.get_sender() == request['owner']:
                    path = self.request_handle(message)
                if path is not None:
                    if path in self.paths:
                        self.close()
                        return
                    self.expired_paths.add(path)
                    if path not in request['paths']:
                        self.close_host_request(request, path)
                return
            message = message.copy()
            message.set_reply_serial(guest_serial)
            if request:
                if kind == Gio.DBusMessageType.ERROR:
                    request['cancelled'].set()
                    self.forget(request)
                else:
                    path = self.request_handle(message)
                    if (path is None or message.get_sender() != request['owner']
                            or path in self.retired or path in self.expired_paths
                            or (path in self.paths and self.paths[path] is not request)):
                        self.close()
                        return
                    self.paths[path] = request
                    request['paths'].add(path)
                    request['handle'] = path
                    self.send(self.down, message)
                    early = self.take_early_response(request)
                    if early is not None:
                        self.incoming(early)
                    return
        if (kind == Gio.DBusMessageType.SIGNAL and message.get_interface() == REQUEST
                and message.get_member() == 'Response'):
            request = self.paths.get(message.get_path())
            if request is None:
                path = message.get_path()
                if path in self.retired or path in self.expired_paths:
                    return
                # A timed-out call can receive a Response on a previously
                # unknown path before the handle reply identifies that path.
                if any(pending and pending.get('expired') and pending['owner'] == message.get_sender()
                       for _, pending in self.upstream_calls.values()):
                    return
            if request:
                # Identity is verified on both sides of the asynchronous call.
                if message.get_sender() != request['owner'] or request['responding']:
                    return
                if self.expire_request(request, self.relay.clock()):
                    return
                if request['handle'] is None:
                    # Hold the first signal until the handle reply is sent.
                    # Expiration before that reply must send only a method error.
                    if request.get('early_response') is None:
                        body = message.get_body()
                        size = queued_message_size(message, body.get_size() if body is not None else 0)
                        with self.relay.queue_lock:
                            if self.relay.queued_bytes + size > self.relay.queue_limit:
                                self.schedule_close_locked()
                                return
                            self.relay.queued_bytes += size
                            request['early_response'] = message, size
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
            result, error = None, getattr(exc, 'code', type(exc).__name__)
        self.finish(request, message, result, error)
        return False

    def finish(self, request, message, result, error):
        if (self.closed or self.close_scheduled or self.requests.get(request['serial']) is not request
                or self.expire_request(request, self.relay.clock())):
            if result:
                self.relay.store.revoke(result)
            return
        self.forget(request)
        forwarded = message.copy()
        if result and not self.relay.store.ready(result):
            error = 'ACCESS_GRANT_REVOKED'
        if request['cancelled'].is_set():
            if result:
                self.relay.store.revoke(result)
            forwarded.set_body(GLib.Variant('(ua{sv})', (1, {})))
        elif error:
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
        now = self.relay.clock()
        for request in list(self.requests.values()):
            self.expire_request(request, now)

    def expire_request(self, request, now):
        if now - request['started'] < REQUEST_TIMEOUT:
            return False
        request['cancelled'].set()
        self.expired_paths.update(request['paths'])
        self.forget(request)
        path = request['handle']
        if path is None:
            if request['serial'] in self.upstream_calls:
                self.upstream_calls[request['serial']] = (None, {
                    'expired': True, 'owner': request['owner'], 'paths': request['paths']})
            reply = request['timeout_reply']
        else:
            reply = Gio.DBusMessage.new_signal(path, REQUEST, 'Response')
            reply.set_sender(request['owner'])
            reply.set_destination(self.up.get_unique_name())
            reply.set_body(GLib.Variant('(ua{sv})', (2, {})))
        try:
            self.down.send_message(reply, Gio.DBusSendMessageFlags.NONE)
        except GLib.Error as exc:
            self.relay.emit(event='timeout-response-failed', code=type(exc).__name__)
            self.schedule_close()
        for close_path in (path,) if path is not None else request['paths']:
            self.close_host_request(request, close_path)
        self.relay.emit(event='request-expired', method=request['method'])
        return True

    def close(self):
        with self.relay.queue_lock:
            if self.closed:
                return False
            self.closed = True
            self.relay.queued_bytes -= sum(size for _, _, size in self.pending.values())
            self.pending.clear()
        for request in self.requests.values():
            request['cancelled'].set()
            self.take_early_response(request)
        self.requests.clear()
        self.paths.clear()
        self.expired_paths.clear()
        self.upstream_calls.clear()
        for connection, identity in self.filters:
            connection.remove_filter(identity)
            if not connection.is_closed():
                connection.close(None, None, None)
        self.relay.clients.discard(self)
        return False


class Relay:
    def __init__(self, directory, guest, shares, *, upstream='unix:path=bus', parent=None,
                 queue_limit=MAX_QUEUED_BYTES, clock=time.monotonic, access_root=None,
                 environment_binding=None, lease_id=None, ready_fd=None):
        self.directory = Path(directory)
        self.upstream = upstream
        self.parent = parent
        self.ready_fd = ready_fd
        self.clients = set()
        self.clock = clock
        # Budget for frames awaiting main-context dispatch or a handle reply.
        # GIO transport buffers and copy state are outside this wire estimate.
        self.queue_lock = threading.Lock()
        self.queue_limit = queue_limit
        self.queued_bytes = 0
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
        self.store = SelectionStore(directory, guest, shares, emit=self.emit, access_root=access_root,
                                    environment_binding=environment_binding, lease_id=lease_id)

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
        try:
            modified = self.store.ledger.path.stat().st_mtime_ns
            if modified != getattr(self, '_ledger_mtime', None):
                if self.store.reconcile_revocations():
                    self._ledger_mtime = modified
        except Exception as exc:
            self.emit(event='access-ledger-unavailable', code=type(exc).__name__)
            self.loop.quit()
            return False
        return True

    def run(self):
        try:
            self.server.start()
            ready = dict(event='ready', protocol_version=PROTOCOL_VERSION, pid=os.getpid(),
                         worker_version=__version__,
                         stamp=stamp(os.getpid()), lease_id=self.store.ledger.header['lease_id'],
                         selection_directory=str(self.store.directory),
                         selection_identity=[self.store.root_identity.st_dev, self.store.root_identity.st_ino])
            if self.ready_fd is not None:
                try:
                    payload = (json.dumps(ready) + '\n').encode()
                    if len(payload) > 4096 or os.write(self.ready_fd, payload) != len(payload):
                        raise RuntimeError('file access readiness message could not be delivered')
                finally:
                    os.close(self.ready_fd)
                    self.ready_fd = None
            self.emit(**ready)
            GLib.timeout_add(500, self.tick)
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
    parser.add_argument('--access-root')
    parser.add_argument('--environment-binding')
    parser.add_argument('--lease-id')
    parser.add_argument('--ready-fd', type=int)
    args = parser.parse_args()
    relay = Relay(Path.cwd(), args.guest, json.loads(args.shares), parent=(args.parent, args.stamp),
                  access_root=args.access_root, environment_binding=(json.loads(args.environment_binding)
                    if args.environment_binding else None), lease_id=args.lease_id, ready_fd=args.ready_fd)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (relay.loop.quit(), False)[1])
    relay.run()


if __name__ == '__main__':
    main()
