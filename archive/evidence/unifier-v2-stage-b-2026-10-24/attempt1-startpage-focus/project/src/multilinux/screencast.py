"""Reusable xdg-desktop-portal ScreenCast client.

The module deliberately does not import PyGObject at import time.  Tests and
offline callers can import it on hosts without ``gi``; the Gio transport is
created lazily by :class:`ScreenCastClient` (or replaced with an injected
transport).  Portal method calls are synchronous because they only return a
Request handle, while the actual user interaction is observed through the
asynchronous ``org.freedesktop.portal.Request.Response`` signal.  The signal
subscription is installed before every method call, so a portal that emits the
response before returning the handle cannot lose it.

Only the portal chooses the shared content.  This client asks for monitor or
window sources, never supplies a restore token, and always requests
``persist_mode`` 0.
"""

import os
import re
import secrets
import time
from dataclasses import dataclass, field

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4

SOURCE_TYPES = {
    "monitor": SOURCE_MONITOR,
    "window": SOURCE_WINDOW,
    "virtual": SOURCE_VIRTUAL,
}

REQUEST_PATH_PREFIX = "/org/freedesktop/portal/desktop/request"
SESSION_PATH_PREFIX = "/org/freedesktop/portal/desktop/session"

_OBJECT_PATH = re.compile(r"/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\Z")
_PATH_ELEMENT = re.compile(r"[A-Za-z0-9_]+\Z")
_MAX_UINT32 = 0xFFFFFFFF
_MAX_UINT64 = 0xFFFFFFFFFFFFFFFF


class ScreenCastError(RuntimeError):
    """Base error for portal screen casting."""


class ScreenCastUnavailable(ScreenCastError):
    """PyGObject, the session bus or the portal itself is unavailable."""


class ScreenCastProtocolError(ScreenCastError):
    """The portal returned malformed data or used an unexpected reply."""


class ScreenCastRequestError(ScreenCastError):
    """A portal request ended with an error response."""

    def __init__(self, message, *, response=None, results=None, portal_error=None):
        super().__init__(message)
        self.response = response
        self.results = results or {}
        self.portal_error = portal_error


class ScreenCastCancelled(ScreenCastRequestError):
    """The user cancelled or denied the portal selection dialog."""


class ScreenCastTimeout(ScreenCastError):
    """A portal Request.Response did not arrive before the deadline."""


def new_handle_token():
    """Return an unguessable token valid as one D-Bus object path element."""
    return "ml_screencast_" + secrets.token_hex(16)


def predicted_handle(kind, unique_name, token):
    """Predict a Request/Session handle for a well-behaved portal.

    Old portals may ignore ``handle_token`` and return a different path.  The
    prediction is only used to check the actual handle and to diagnose a
    mismatch; responses are always matched against the exact handle returned by
    the portal.
    """
    if not unique_name or not _PATH_ELEMENT.fullmatch(token):
        return None
    sender = unique_name[1:] if unique_name.startswith(":") else unique_name
    sender = sender.replace(".", "_")
    if not _PATH_ELEMENT.fullmatch(sender):
        return None
    if kind == "session":
        return f"{SESSION_PATH_PREFIX}/{sender}/{token}"
    return f"{REQUEST_PATH_PREFIX}/{sender}/{token}"


def _valid_object_path(path):
    return isinstance(path, str) and bool(_OBJECT_PATH.fullmatch(path))


def _plain(value):
    """Convert GLib-unpacked data to JSON-friendly values without binaries."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary omitted>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass(frozen=True)
class SourceStream:
    """One stream returned by ScreenCast.Start.

    ``node_id`` exists since the first interface version.  ``serial`` is the
    version 6 ``pipewire-serial`` property and is the durable target for
    PipeWire consumers; it is optional for version 5 portals.
    """

    node_id: int
    serial: int | None = None
    properties: dict = field(default_factory=dict)

    @classmethod
    def from_portal(cls, raw):
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ScreenCastProtocolError(
                f"ScreenCast.Start returned an invalid stream tuple: {raw!r}")
        node_id, properties = raw
        if (isinstance(node_id, bool) or not isinstance(node_id, int)
                or not 1 <= node_id <= _MAX_UINT32):
            raise ScreenCastProtocolError(
                f"ScreenCast.Start returned an invalid PipeWire node id: {node_id!r}")
        if not isinstance(properties, dict):
            raise ScreenCastProtocolError(
                f"ScreenCast.Start returned invalid stream properties: {properties!r}")
        clean = {str(key): _plain(value) for key, value in properties.items()}
        serial = clean.get("pipewire-serial")
        if serial is None:
            serial = clean.get("pipewire_serial")
        if serial is not None:
            if (isinstance(serial, bool) or not isinstance(serial, int)
                    or not 0 <= serial <= _MAX_UINT64):
                raise ScreenCastProtocolError(
                    f"ScreenCast.Start returned an invalid pipewire-serial: {serial!r}")
        return cls(node_id=node_id, serial=serial, properties=clean)

    def as_metadata(self):
        return {
            "node_id": self.node_id,
            "pipewire_serial": self.serial,
            "properties": _plain(self.properties),
        }


def _load_gi():
    try:
        from gi.repository import Gio, GLib
    except (ImportError, ValueError, OSError) as exc:
        raise ScreenCastUnavailable(
            "PyGObject with Gio/GLib is required for the desktop ScreenCast portal; "
            "offline imports and injected transports do not need it") from exc
    return Gio, GLib


def _extract_unix_fd(fd_list, handle_index):
    """Extract the duplicated OS fd selected by a D-Bus ``h`` value.

    A D-Bus ``h`` is an index into the message's UnixFDList, not an OS file
    descriptor.  ``Gio.UnixFDList.get`` returns a duplicate of the descriptor
    at that index; the duplicate is owned by the caller.  This helper rejects
    out-of-range indexes instead of ever returning the index itself.
    """
    if isinstance(handle_index, bool) or not isinstance(handle_index, int):
        raise ScreenCastProtocolError(
            f"portal returned a non-integer Unix fd index: {handle_index!r}")
    if handle_index < 0:
        raise ScreenCastProtocolError(
            f"portal returned a negative Unix fd index: {handle_index}")
    try:
        length = int(fd_list.get_length())
    except Exception as exc:
        raise ScreenCastProtocolError(
            f"portal did not return a usable UnixFDList: {exc}") from exc
    if handle_index >= length:
        raise ScreenCastProtocolError(
            f"portal Unix fd index {handle_index} is outside a list of {length} fd(s)")
    try:
        fd = fd_list.get(handle_index)
    except Exception as exc:
        raise ScreenCastProtocolError(
            f"could not extract Unix fd index {handle_index}: {exc}") from exc
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise ScreenCastProtocolError(
            f"portal UnixFDList returned no fd for index {handle_index}: {fd!r}")
    return fd


def _drain_fd_list(fd_list):
    """Close the fd list's own descriptors once get() made our duplicate.

    GUnixFDList owns descriptors that are separate from the duplicate returned
    by get().  ``steal_fds`` transfers them so they can be closed now instead
    of waiting for Python garbage collection.  The API is guarded because a
    few older bindings do not expose it.
    """
    if fd_list is None:
        return
    try:
        original_fds = fd_list.steal_fds()
    except Exception:
        return
    for fd in original_fds or ():
        if isinstance(fd, int) and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


class GioPortalTransport:
    """Gio/GLib transport for the ScreenCast portal.

    The constructor is the only place that imports ``gi``.  Replace this object
    through ``ScreenCastClient(transport=...)`` in offline tests.
    """

    def __init__(self, bus_address=None):
        self._gio, self._glib = _load_gi()
        self._bus_address = bus_address
        self._own_connection = bool(bus_address)
        self._subscriptions = set()
        self.closed = False
        Gio, GLib = self._gio, self._glib
        try:
            if bus_address:
                flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                         | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
                self.connection = Gio.DBusConnection.new_for_address_sync(
                    bus_address, flags, None, None)
            else:
                self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            raise ScreenCastUnavailable(
                f"cannot connect to the ScreenCast portal D-Bus bus: {exc.message}") from exc
        except Exception as exc:
            raise ScreenCastUnavailable(
                f"cannot connect to the ScreenCast portal D-Bus bus: {exc}") from exc
        self.unique_name = None
        try:
            self.unique_name = self.connection.get_unique_name()
        except Exception:
            pass

    def _variant_parameters(self, method, parameters):
        GLib = self._glib
        if method == "CreateSession":
            if len(parameters) != 1:
                raise ScreenCastProtocolError("CreateSession expects one options dict")
            return GLib.Variant.new_tuple(self._vardict(parameters[0]))
        if method in {"SelectSources", "OpenPipeWireRemote"}:
            if len(parameters) != 2:
                raise ScreenCastProtocolError(f"{method} expects a session and options")
            return GLib.Variant.new_tuple(
                GLib.Variant("o", str(parameters[0])),
                self._vardict(parameters[1]))
        if method == "Start":
            if len(parameters) != 3:
                raise ScreenCastProtocolError("Start expects session, parent and options")
            return GLib.Variant.new_tuple(
                GLib.Variant("o", str(parameters[0])),
                GLib.Variant("s", str(parameters[1])),
                self._vardict(parameters[2]))
        if method == "Get":
            if len(parameters) != 2:
                raise ScreenCastProtocolError("Properties.Get expects two strings")
            return GLib.Variant.new_tuple(
                GLib.Variant("s", str(parameters[0])),
                GLib.Variant("s", str(parameters[1])))
        if method == "Close":
            return None
        raise ScreenCastProtocolError(f"unsupported portal method: {method}")

    def _vardict(self, values):
        if not isinstance(values, dict):
            raise ScreenCastProtocolError(f"portal options must be a dict: {values!r}")
        variants = {}
        for key, value in values.items():
            variants[str(key)] = self._variant_for(value)
        return self._glib.Variant("a{sv}", variants)

    def _variant_for(self, value):
        if hasattr(value, "get_type_string"):
            return value
        if isinstance(value, bool):
            return self._glib.Variant("b", value)
        if isinstance(value, int):
            if 0 <= value <= _MAX_UINT32:
                return self._glib.Variant("u", value)
            return self._glib.Variant("t", value)
        if isinstance(value, str):
            return self._glib.Variant("s", value)
        if isinstance(value, dict):
            return self._vardict(value)
        raise ScreenCastProtocolError(
            f"cannot encode portal option value of type {type(value).__name__}")

    def _reply_type(self, method):
        if method in {"CreateSession", "SelectSources", "Start"}:
            return self._glib.VariantType.new("(o)")
        if method == "Get":
            return self._glib.VariantType.new("(v)")
        return None

    def _request_error(self, method, exc):
        remote = None
        try:
            remote = self._gio.DBusError.get_remote_error(exc)
        except Exception:
            pass
        message = getattr(exc, "message", str(exc))
        return ScreenCastRequestError(
            f"{method} portal call failed: {message}", portal_error=remote)

    def call(self, path, interface, method, parameters, timeout_ms):
        if self.closed:
            raise ScreenCastUnavailable("portal transport is closed")
        try:
            result = self.connection.call_sync(
                PORTAL_BUS, path, interface, method,
                self._variant_parameters(method, parameters),
                self._reply_type(method),
                self._gio.DBusCallFlags.NONE,
                max(1, int(timeout_ms)), None)
        except self._glib.Error as exc:
            raise self._request_error(method, exc) from exc
        except ScreenCastError:
            raise
        except Exception as exc:
            raise ScreenCastRequestError(f"{method} portal call failed: {exc}") from exc
        if result is None:
            return None
        try:
            values = result.unpack()
        except Exception as exc:
            raise ScreenCastProtocolError(
                f"{method} returned an undecodable reply: {exc}") from exc
        if isinstance(values, tuple) and values:
            return values[0]
        return None

    def call_with_fd(self, path, interface, method, parameters, timeout_ms):
        if self.closed:
            raise ScreenCastUnavailable("portal transport is closed")
        Gio, GLib = self._gio, self._glib
        fd_list = None
        try:
            result, fd_list = self.connection.call_with_unix_fd_list_sync(
                PORTAL_BUS, path, interface, method,
                self._variant_parameters(method, parameters),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE,
                max(1, int(timeout_ms)), None, None)
        except GLib.Error as exc:
            raise self._request_error(method, exc) from exc
        except ScreenCastError:
            raise
        except Exception as exc:
            raise ScreenCastRequestError(f"{method} portal call failed: {exc}") from exc
        try:
            values = result.unpack()
            if not isinstance(values, tuple) or len(values) != 1:
                raise ScreenCastProtocolError(
                    f"{method} returned an unexpected fd reply: {values!r}")
            return _extract_unix_fd(fd_list, values[0])
        finally:
            _drain_fd_list(fd_list)

    def subscribe(self, callback):
        """Watch every portal Request.Response and report its object path."""
        if self.closed:
            raise ScreenCastUnavailable("portal transport is closed")

        def handler(_connection, _sender, path, _interface, _signal, parameters):
            try:
                values = parameters.unpack()
                code = int(values[0])
                results = values[1] if len(values) > 1 and isinstance(values[1], dict) else {}
            except Exception as exc:
                path = path or ""
                code, results = -1, {"error": f"undecodable Response: {exc}"}
            try:
                callback(path, code, results)
            except Exception:
                # A callback failure must not escape into the GLib main loop.
                pass

        subscription = self.connection.signal_subscribe(
            PORTAL_BUS, REQUEST_INTERFACE, "Response", None, None,
            self._gio.DBusSignalFlags.NONE, handler)
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription):
        if subscription is None:
            return
        self._subscriptions.discard(subscription)
        try:
            self.connection.signal_unsubscribe(subscription)
        except Exception:
            pass

    def wait(self, predicate, timeout_ms):
        """Pump the GLib main context until predicate() or the deadline."""
        if predicate():
            return True
        timeout_ms = max(0, int(timeout_ms))
        if timeout_ms == 0:
            return False
        GLib = self._glib
        loop = GLib.MainLoop()
        state = {"done": predicate(), "expired": False}
        sources = {"poll": None, "deadline": None, "poll_alive": True, "deadline_alive": True}

        def check():
            if state["done"] or predicate():
                state["done"] = True
                sources["poll_alive"] = False
                loop.quit()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        def expire():
            state["expired"] = True
            sources["deadline_alive"] = False
            loop.quit()
            return GLib.SOURCE_REMOVE

        sources["poll"] = GLib.timeout_add(10, check)
        sources["deadline"] = GLib.timeout_add(max(1, timeout_ms), expire)
        try:
            # A response may already have been queued before wait() is called.
            if state["done"]:
                loop.quit()
            else:
                loop.run()
        finally:
            if sources["poll_alive"]:
                GLib.source_remove(sources["poll"])
                sources["poll_alive"] = False
            if sources["deadline_alive"]:
                GLib.source_remove(sources["deadline"])
                sources["deadline_alive"] = False
        return state["done"] or predicate()

    def close_request(self, handle, timeout_ms=2000):
        self.call(handle, REQUEST_INTERFACE, "Close", [], timeout_ms)

    def close_session(self, handle, timeout_ms=2000):
        self.call(handle, SESSION_INTERFACE, "Close", [], timeout_ms)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for subscription in list(self._subscriptions):
            self.unsubscribe(subscription)
        if self._own_connection:
            try:
                self.connection.close_sync(None)
            except Exception:
                pass


class ScreenCastClient:
    """Create, select, start and expose one portal ScreenCast session.

    The instance is a context manager.  It owns all remote PipeWire fds it
    opens; callers must keep an fd alive while their consumer uses it and may
    release it earlier with :meth:`close_remote`.
    """

    def __init__(self, bus_address=None, *, transport=None):
        self._transport = (GioPortalTransport(bus_address)
                           if transport is None else transport)
        self.unique_name = getattr(self._transport, "unique_name", None)
        self.events = []
        self.warnings = []
        self.version = None
        self.session_handle = None
        self.streams = []
        self.selection = {}
        self._responses = {}
        self._remote_fds = set()
        self.closed = False

    @property
    def open_remote_fds(self):
        return tuple(sorted(self._remote_fds))

    def _event(self, phase, status, **details):
        event = {"phase": phase, "status": status, "at": round(time.time(), 3)}
        event.update({key: _plain(value) for key, value in details.items()})
        self.events.append(event)

    def _warn(self, message, **details):
        item = {"message": message, "at": round(time.time(), 3)}
        item.update({key: _plain(value) for key, value in details.items()})
        self.warnings.append(item)

    def _on_response(self, path, code, results):
        if not _valid_object_path(path):
            self._warn("ignored Request.Response with an invalid path", path=path)
            return
        if path in self._responses:
            self._warn("ignored duplicate Request.Response", handle=path)
            return
        self._responses[path] = (code, dict(results) if isinstance(results, dict) else {})

    def _request(self, method, arguments, timeout_ms):
        token = new_handle_token()
        options = dict(arguments[-1])
        options["handle_token"] = token
        arguments = list(arguments[:-1]) + [options]
        expected = predicted_handle("request", self.unique_name, token)
        subscription = None
        actual = None
        responded = False
        timeout_ms = max(1, int(timeout_ms))
        self._event("portal_request", "started", method=method, handle_token=token)
        try:
            # Subscribe before the call: the portal is allowed to emit
            # Response before it returns the request handle.
            subscription = self._transport.subscribe(self._on_response)
            self._event("portal_request", "subscribed", method=method,
                        expected_handle=expected)
            actual = self._transport.call(
                PORTAL_PATH, SCREENCAST_INTERFACE, method, arguments, timeout_ms)
            actual = str(actual) if actual is not None else ""
            if not _valid_object_path(actual):
                raise ScreenCastProtocolError(
                    f"{method} returned an invalid Request handle: {actual!r}")
            if expected and actual != expected:
                self._warn(
                    "portal returned a different Request handle than the token prediction; "
                    "matching Response by the actual handle",
                    method=method, expected_handle=expected, actual_handle=actual)
                self._event("portal_request", "handle_mismatch", method=method,
                            expected_handle=expected, actual_handle=actual)
            self._event("portal_request", "called", method=method,
                        actual_handle=actual)
            if not self._transport.wait(
                    lambda: actual in self._responses, timeout_ms):
                raise ScreenCastTimeout(
                    f"{method} did not produce Request.Response on {actual} "
                    f"within {timeout_ms} ms")
            code, results = self._responses.pop(actual)
            responded = True
            if code == 0:
                self._event("portal_request", "completed", method=method,
                            actual_handle=actual, response=code)
                return results
            if code == 1:
                self._event("portal_request", "cancelled", method=method,
                            actual_handle=actual, response=code)
                raise ScreenCastCancelled(
                    f"{method}: the user cancelled or denied the screen cast request "
                    f"(portal Response code 1)",
                    response=code, results=results)
            if code == 2:
                self._event("portal_request", "failed", method=method,
                            actual_handle=actual, response=code)
                raise ScreenCastRequestError(
                    f"{method}: the portal request failed (Response code 2)",
                    response=code, results=results)
            self._event("portal_request", "failed", method=method,
                        actual_handle=actual, response=code)
            raise ScreenCastProtocolError(
                f"{method}: unexpected Request.Response code {code}")
        except ScreenCastError as exc:
            self._event("portal_request", "error", method=method,
                        actual_handle=actual, error=str(exc))
            raise
        finally:
            if actual and not responded:
                try:
                    self._transport.close_request(actual, 2000)
                    self._event("portal_request", "closed_unanswered",
                                method=method, actual_handle=actual)
                except Exception as exc:
                    self._warn("could not close unanswered portal request",
                               handle=actual, error=str(exc))
            if subscription is not None:
                try:
                    self._transport.unsubscribe(subscription)
                except Exception as exc:
                    self._warn("could not remove Request.Response subscription",
                               error=str(exc))
            if expected and expected != actual:
                self._responses.pop(expected, None)

    def _read_version(self, timeout_ms):
        try:
            value = self._transport.call(
                PORTAL_PATH, PROPERTIES_INTERFACE, "Get",
                [SCREENCAST_INTERFACE, "version"], timeout_ms)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ScreenCastProtocolError(
                    f"portal returned a non-numeric ScreenCast version: {value!r}")
            version = int(value)
            if version < 0:
                raise ScreenCastProtocolError(
                    f"portal returned a negative ScreenCast version: {version}")
            self.version = version
            self._event("portal", "version", version=version)
        except Exception as exc:
            # Version is informational.  An older portal or a test transport
            # without Properties support must not break the session.
            self._warn("could not read ScreenCast interface version", error=str(exc))

    def create_session(self, *, timeout_ms=120000):
        if self.session_handle:
            raise ScreenCastProtocolError("a ScreenCast session is already open")
        self._read_version(min(int(timeout_ms), 5000))
        session_token = new_handle_token()
        results = self._request(
            "CreateSession",
            [{"session_handle_token": session_token,
              "handle_token": new_handle_token()}],
            timeout_ms)
        handle = results.get("session_handle")
        if not _valid_object_path(handle):
            raise ScreenCastProtocolError(
                f"CreateSession returned an invalid session handle: {handle!r}")
        self.session_handle = handle
        self._event("session", "created", session_handle=handle,
                    version=self.version)
        return handle

    def select_sources(self, source="window", *, timeout_ms=120000):
        if not self.session_handle:
            raise ScreenCastProtocolError("SelectSources requires an open session")
        if source not in SOURCE_TYPES:
            raise ScreenCastProtocolError(
                f"unsupported source type: {source!r} (use monitor or window)")
        options = {
            "types": SOURCE_TYPES[source],
            "multiple": False,
            "persist_mode": 0,
        }
        self._request("SelectSources", [self.session_handle, options], timeout_ms)
        self.selection = {
            "source": source,
            "types": SOURCE_TYPES[source],
            "multiple": False,
            "persist_mode": 0,
        }
        self._event("selection", "selected", **self.selection)
        return self.selection

    def start(self, *, source="window", parent_window="", selection_timeout_ms=120000):
        """Run CreateSession -> SelectSources -> Start and return streams."""
        if self.session_handle:
            raise ScreenCastProtocolError("a ScreenCast session is already open")
        if source not in SOURCE_TYPES:
            raise ScreenCastProtocolError(
                f"unsupported source type: {source!r} (use monitor or window)")
        timeout_ms = max(1, int(selection_timeout_ms))
        try:
            self.create_session(timeout_ms=timeout_ms)
            self.select_sources(source, timeout_ms=timeout_ms)
            results = self._request(
                "Start",
                [self.session_handle, str(parent_window or ""), {}],
                timeout_ms)
            raw_streams = results.get("streams")
            if not isinstance(raw_streams, (list, tuple)) or not raw_streams:
                raise ScreenCastProtocolError(
                    "Start succeeded but returned no streams")
            streams = [SourceStream.from_portal(raw) for raw in raw_streams]
            self.streams = streams
            self._event("session", "started",
                        streams=[stream.as_metadata() for stream in streams],
                        restore_token_present=bool(results.get("restore_token")))
            return streams
        except BaseException:
            # A half-built session must not outlive the failed start.
            self.close()
            raise

    def open_pipewire_remote(self, *, timeout_ms=10000):
        if not self.session_handle:
            raise ScreenCastProtocolError(
                "OpenPipeWireRemote requires an open session")
        if self.closed:
            raise ScreenCastProtocolError("the ScreenCast client is closed")
        self._event("pipewire_remote", "opening")
        fd = self._transport.call_with_fd(
            PORTAL_PATH, SCREENCAST_INTERFACE, "OpenPipeWireRemote",
            [self.session_handle, {}], max(1, int(timeout_ms)))
        if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
            raise ScreenCastProtocolError(
                f"OpenPipeWireRemote returned an invalid fd: {fd!r}")
        try:
            os.fstat(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise ScreenCastProtocolError(
                f"OpenPipeWireRemote returned a non-open fd: {exc}") from exc
        self._remote_fds.add(fd)
        self._event("pipewire_remote", "opened", fd=fd)
        return fd

    def close_remote(self, fd):
        """Close one fd owned by this client.  Returns True if it was tracked."""
        if fd not in self._remote_fds:
            return False
        self._remote_fds.discard(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        self._event("pipewire_remote", "closed", fd=fd)
        return True

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.session_handle:
            handle = self.session_handle
            self.session_handle = None
            try:
                self._transport.close_session(handle, 2000)
                self._event("session", "closed", session_handle=handle)
            except Exception as exc:
                self._warn("could not close portal session", handle=handle,
                           error=str(exc))
        for fd in sorted(self._remote_fds):
            try:
                os.close(fd)
            except OSError:
                pass
            self._event("pipewire_remote", "closed", fd=fd)
        self._remote_fds.clear()
        try:
            self._transport.close()
        except Exception as exc:
            self._warn("could not close portal transport", error=str(exc))
        self._event("client", "closed")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
        return False
