"""Read-only FileRef resolution: boundaries, symlinks, root replacement, versions."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from multilinux import control as control_module
from multilinux.control import Control
from multilinux.errors import MultilinuxError


class FileReference(unittest.TestCase):
    def _control(self, root: Path):
        data = root / "data"
        data.mkdir()
        control = Control(root)
        registration = control.add_root(str(data))
        return control, registration, data

    def test_file_ref_carries_root_role_management_domain_and_fd_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            control, registration, data = self._control(Path(temporary))
            path = data / "note.txt"
            path.write_text("hello\n", encoding="utf-8")
            reference = control.resolve_file(registration["root_uuid"], "note.txt", "project")
            self.assertEqual(reference["root_uuid"], registration["root_uuid"])
            self.assertEqual(reference["relative_path"], "note.txt")
            self.assertEqual(reference["role"], "project")
            self.assertEqual(reference["management_domain"], "host-project")
            self.assertEqual(reference["access_policy"], "read-write")
            observed = reference["observed_version"]
            self.assertEqual(observed["file_type"], "regular")
            self.assertEqual(observed["size"], 6)
            self.assertEqual(observed["mtime"], path.stat().st_mtime)
            self.assertEqual(observed["ctime"], path.stat().st_ctime)
            self.assertEqual(observed["ino"], path.stat().st_ino)
            self.assertEqual(observed["dev"], path.stat().st_dev)
            self.assertEqual(reference["implemented_operations"], ["observe"])
            self.assertTrue(reference["capabilities"]["observe"])
            for operation in ("read", "write", "read_content", "write_content",
                              "create", "delete", "copy", "move", "execute"):
                self.assertFalse(reference["capabilities"][operation])
            self.assertIsNone(reference["execution"]["interpreter"])
            self.assertIsNone(reference["execution"]["environment"])
            for role in ("document", "artifact"):
                self.assertEqual(
                    control.resolve_file(registration["root_uuid"], "note.txt", role)["role"],
                    role)

    def test_relative_boundaries_and_every_symlink_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control, registration, data = self._control(root)
            (data / "file.txt").write_text("inside", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            os.symlink(data / "file.txt", data / "link.txt")
            os.symlink(outside, data / "dirlink")
            uuid = registration["root_uuid"]
            refused = (
                "", "/etc/passwd", "../outside/secret.txt", "sub/../../outside/secret.txt",
                ".", "./file.txt", "a//b", "dirlink/secret.txt", "link.txt",
            )
            for relative in refused:
                with self.subTest(relative=relative), self.assertRaises(MultilinuxError):
                    control.resolve_file(uuid, relative)
            self.assertEqual(
                control.resolve_file(uuid, "file.txt", "source")["role"], "source")
            with self.assertRaises(MultilinuxError):
                control.resolve_file(uuid, "file.txt", "not a role")
            with self.assertRaises(MultilinuxError):
                control.resolve_path(str(root / ".." / "etc" / "passwd"))

    def test_nested_root_selection_uses_the_longest_registered_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outer = root / "project"
            inner = outer / "vendor"
            inner.mkdir(parents=True)
            (inner / "lib.c").write_text("int x;\n", encoding="utf-8")
            control = Control(root)
            outer_record = control.add_root(str(outer))
            inner_record = control.add_root(str(inner))
            reference = control.resolve_path(str(inner / "lib.c"))
            self.assertEqual(reference["root_uuid"], inner_record["root_uuid"])
            self.assertEqual(reference["relative_path"], "lib.c")
            other = control.resolve_path(str(outer / "vendor" / "lib.c"))
            self.assertEqual(other["root_uuid"], inner_record["root_uuid"])
            self.assertNotEqual(inner_record["root_uuid"], outer_record["root_uuid"])

    def test_root_path_replacement_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control, registration, data = self._control(root)
            (data / "file.txt").write_text("old", encoding="utf-8")
            control.resolve_file(registration["root_uuid"], "file.txt")
            moved = root / "old-data"
            data.rename(moved)
            data.mkdir()
            (data / "file.txt").write_text("new", encoding="utf-8")
            with self.assertRaisesRegex(MultilinuxError, "replaced"):
                control.resolve_file(registration["root_uuid"], "file.txt")

    def test_atomic_editor_replace_produces_a_new_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            control, registration, data = self._control(Path(temporary))
            path = data / "document.txt"
            path.write_text("first\n", encoding="utf-8")
            first = control.resolve_file(registration["root_uuid"], "document.txt")
            temporary_path = data / ".document.txt.swp"
            temporary_path.write_text("second version\n", encoding="utf-8")
            os.replace(temporary_path, path)
            second = control.resolve_file(registration["root_uuid"], "document.txt")
            self.assertEqual(second["relative_path"], first["relative_path"])
            self.assertNotEqual(second["observed_version"]["ino"],
                                first["observed_version"]["ino"])
            self.assertEqual(second["observed_version"]["size"], len("second version\n"))
            self.assertNotEqual(second["observed_version"], first["observed_version"])

    @unittest.skipUnless(os.path.exists("/proc/version"), "needs /proc/version")
    def test_cross_mount_boundary_inside_root_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            control = Control(Path(temporary))
            registration = control.add_root("/")
            with self.assertRaisesRegex(MultilinuxError, "mount boundary|device boundary"):
                control.resolve_file(registration["root_uuid"], "proc/version")

    def test_missing_mount_identity_refuses_the_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            control, registration, data = self._control(Path(temporary))
            (data / "file.txt").write_text("inside", encoding="utf-8")
            with patch.object(
                    control_module, "_mount_id",
                    side_effect=MultilinuxError("cannot verify the mount boundary")):
                with self.assertRaisesRegex(MultilinuxError, "cannot verify the mount boundary"):
                    control.resolve_file(registration["root_uuid"], "file.txt")

    def test_nested_root_policy_is_not_bypassed_by_an_explicit_parent_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_path = root / "project"
            child_path = parent_path / "vendor"
            child_path.mkdir(parents=True)
            (parent_path / "README").write_text("parent\n", encoding="utf-8")
            (child_path / "lib.c").write_text("child\n", encoding="utf-8")
            control = Control(root)
            parent = control.add_root(str(parent_path), access_policy="read-write")
            old_ref = control.resolve_file(parent["root_uuid"], "README")
            self.assertEqual(old_ref["access_policy"], "read-write")
            child = control.add_root(str(child_path), access_policy="read-only")

            with self.assertRaisesRegex(MultilinuxError, "nested data root"):
                control.resolve_file(parent["root_uuid"], "vendor/lib.c")
            direct = control.resolve_file(child["root_uuid"], "lib.c")
            automatic = control.resolve_path(str(child_path / "lib.c"))
            self.assertEqual(direct["root_uuid"], child["root_uuid"])
            self.assertEqual(automatic["root_uuid"], child["root_uuid"])
            self.assertEqual(direct["access_policy"], "read-only")
            self.assertEqual(automatic["access_policy"], "read-only")
            self.assertEqual(automatic["relative_path"], "lib.c")
            self.assertEqual(old_ref, control.resolve_file(parent["root_uuid"], "README"))

            control.remove_root(child["root_uuid"])
            after_removal = control.resolve_file(parent["root_uuid"], "vendor/lib.c")
            self.assertEqual(after_removal["root_uuid"], parent["root_uuid"])
            self.assertEqual(after_removal["access_policy"], "read-write")

    def test_hardlinked_regular_file_is_refused_without_alias_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first"
            second_path = root / "second"
            first_path.mkdir()
            second_path.mkdir()
            source = first_path / "shared.txt"
            source.write_text("shared bytes\n", encoding="utf-8")
            os.link(source, second_path / "alias.txt")
            (first_path / "plain.txt").write_text("plain\n", encoding="utf-8")
            control = Control(root)
            first = control.add_root(str(first_path))
            second = control.add_root(str(second_path))
            self.assertGreater(source.stat().st_nlink, 1)
            for registration, relative in ((first, "shared.txt"), (second, "alias.txt")):
                with self.subTest(root=registration["root_uuid"]), \
                     self.assertRaisesRegex(MultilinuxError, "hard-linked"):
                    control.resolve_file(registration["root_uuid"], relative)
            ordinary = control.resolve_file(first["root_uuid"], "plain.txt")
            self.assertEqual(ordinary["observed_version"]["nlink"], 1)


if __name__ == "__main__":
    unittest.main()
