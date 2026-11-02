"""Independent directory-link boundaries for raw roots and real-view semantics."""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from unifier.errors import UnifierError
from unifier.guest_paths import list_guest_directories


class GuestDirectoryBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "a").mkdir()
        (self.root / "b/child").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_relative_parent_link_is_listed_and_followed(self):
        (self.root / "a/link").symlink_to("../b")
        self.assertEqual(list_guest_directories(self.root, "/a")["entries"], ["link"])
        self.assertEqual(list_guest_directories(self.root, "/a/link")["entries"], ["child"])

    def test_raw_absolute_link_uses_guest_root(self):
        (self.root / "abs").symlink_to("/b")
        self.assertEqual(list_guest_directories(self.root, "/abs")["entries"], ["child"])

    def test_projected_absolute_mount_link_is_stripped_once(self):
        (self.root / "bin").symlink_to(str(self.root / "b"))
        self.assertEqual(list_guest_directories(
            self.root, "/bin", projected=True)["entries"], ["child"])
        self.assertIn("bin", list_guest_directories(self.root, projected=True)["entries"])

    def test_projected_mode_refuses_link_outside_exact_mount(self):
        (self.root / "escape").symlink_to(str(self.root) + "-other/b")
        with self.assertRaises(UnifierError):
            list_guest_directories(self.root, "/escape", projected=True)

    def test_raw_mode_never_treats_a_host_prefix_as_authority(self):
        (self.root / "host-looking").symlink_to(str(self.root / "b"))
        with self.assertRaises((OSError, UnifierError)):
            list_guest_directories(self.root, "/host-looking")

    def test_intermediate_link_then_parent_keeps_component_order(self):
        (self.root / "a/jump").symlink_to("../b")
        (self.root / "root-only").mkdir()
        self.assertIn("root-only", list_guest_directories(
            self.root, "/a/jump/..")["entries"])

    def test_parent_at_guest_root_cannot_reach_host(self):
        self.assertEqual(list_guest_directories(self.root, "/../../../b")["entries"], ["child"])

    def test_loop_and_file_links_are_not_reported_as_directories(self):
        (self.root / "a/loop").symlink_to("loop")
        (self.root / "file").write_text("not a directory")
        (self.root / "a/filelink").symlink_to("../file")
        self.assertEqual(list_guest_directories(self.root, "/a")["entries"], [])

    def test_resource_errors_are_not_hidden_as_missing_entries(self):
        (self.root / "a/link").symlink_to("../b")
        original = os.open

        def limited(path, flags, *args, **kwargs):
            if path == "b":
                raise OSError(errno.EMFILE, "injected FD limit")
            return original(path, flags, *args, **kwargs)

        with patch("unifier.guest_paths.os.open", side_effect=limited):
            with self.assertRaises(UnifierError):
                list_guest_directories(self.root, "/a")

    def test_success_and_failure_release_descriptors(self):
        before = len(list(Path("/proc/self/fd").iterdir()))
        for _ in range(30):
            list_guest_directories(self.root, "/b")
            with self.assertRaises(OSError):
                list_guest_directories(self.root, "/missing")
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)


if __name__ == "__main__":
    unittest.main()
