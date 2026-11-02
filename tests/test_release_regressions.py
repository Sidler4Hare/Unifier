"""Portability and live-file boundaries found on real test hosts."""

import errno
import fcntl
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier.guest_paths import list_guest_directories
from unifier.lease import WriteLease
from unifier.errors import UnifierError
from test_projection_support import fixture


class ReleaseRegressions(unittest.TestCase):
    def test_native_lease_without_python_f_setown_ex_symbol(self):
        # Remove the optional Python name, while still asking the real kernel
        # for the thread-directed lease and checking restoration of the mask.
        symbol = getattr(fcntl, "F_SETOWN_EX", None)
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with tempfile.TemporaryFile() as stream:
            lease = WriteLease(stream.fileno(), "Python 3.12 boundary")
            try:
                if symbol is not None:
                    del fcntl.F_SETOWN_EX
                lease.acquire()
                self.assertEqual(lease.observe(), "held")
            finally:
                lease.release()
                if symbol is not None:
                    fcntl.F_SETOWN_EX = symbol
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), before)

    def test_denied_link_target_does_not_hide_readable_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/readable").mkdir(parents=True)
            (root / "etc/mtab").symlink_to("/proc/mounts")
            original = os.readlink

            def restricted(name, *args, **kwargs):
                if name == "mtab":
                    raise OSError(denial, "restricted capability target")
                return original(name, *args, **kwargs)

            for denial in (errno.EACCES, errno.EPERM, errno.EOPNOTSUPP):
                with self.subTest(errno=denial), patch(
                        "unifier.guest_paths.os.readlink", side_effect=restricted):
                    self.assertEqual(list_guest_directories(
                        root, "/etc")["entries"], ["readable"])
                    with self.assertRaises(OSError) as caught:
                        list_guest_directories(root, "/etc/mtab")
                    self.assertEqual(caught.exception.errno, denial)

    def test_link_io_error_is_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "link").symlink_to("missing")
            with patch("unifier.guest_paths.os.readlink",
                       side_effect=OSError(errno.EIO, "I/O failure")):
                with self.assertRaises(UnifierError):
                    list_guest_directories(root)

    def test_identical_metadata_still_invalidates_pages_before_reply(self):
        with fixture() as (root, backend, server):
            path = root / "file"
            path.write_bytes(b"OLD")
            node = server.lookup("file")
            fh = server.open_file(node)
            metadata = backend.node_attr_by_id(node, fh)
            try:
                self.assertEqual(server.read(fh, 0, 3)[1], b"OLD")
                path.write_bytes(b"NEW")
                # Model a coarse-timestamp filesystem deterministically,
                # independently of the machine running this regression.
                with patch.object(backend, "node_attr", return_value=metadata):
                    self.assertEqual(server.getattr(node, fh)[2], 0)
                self.assertEqual([e[0] for e in server.events], ["notify", "reply"])
                self.assertEqual(server.read(fh, 0, 3)[1], b"NEW")
            finally:
                server.release(fh)


if __name__ == "__main__":
    unittest.main()
