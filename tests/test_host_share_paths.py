"""Tests over private temporary files for the host-share path boundary."""

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from unifier import host_share_paths
from unifier.errors import UnifierError
from unifier.host_share_paths import open_host_share_path
from unifier.host_share_policy import HostSharePolicy


class HostSharePathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = HostSharePolicy((str(self.root),))

    def assert_refused(self, path, *, policy=None, write=False):
        with self.assertRaises(UnifierError):
            with open_host_share_path(policy or self.policy, str(path), write=write):
                self.fail("an unauthorized path passed verification")

    def test_descriptor_pins_the_requested_directory_or_file(self):
        target = self.root / "项目 记录.txt"
        target.write_text("original", encoding="utf-8")
        for path in (self.root, target):
            with self.subTest(path=path):
                expected = path.stat()
                with open_host_share_path(self.policy, str(path)) as descriptor:
                    observed = os.fstat(descriptor)
                    self.assertEqual((observed.st_dev, observed.st_ino),
                                     (expected.st_dev, expected.st_ino))
                    if path == target:
                        target.rename(self.root / "saved")
                        target.touch()
                        self.assertEqual(os.fstat(descriptor).st_ino, expected.st_ino)
                        self.assertNotEqual(target.stat().st_ino, expected.st_ino)
                with self.assertRaises(OSError) as error:
                    os.fstat(descriptor)
                self.assertEqual(error.exception.errno, errno.EBADF)

    def test_policy_refuses_before_opening(self):
        private = self.root / "private"
        admin = self.root / "admin"
        policy = HostSharePolicy((str(self.root),), blacklist=(str(private),),
                                 admin_roots=(str(admin),))
        with mock.patch.object(host_share_paths, "_openat2") as open_at:
            self.assert_refused(private / "file", policy=policy)
            self.assert_refused(admin / "file", policy=policy)
            self.assert_refused(self.root, policy=policy, write=True)
            self.assert_refused(str(self.root) + "/../outside", policy=policy)
            self.assert_refused(str(self.root) + "-sibling/file", policy=policy)
            open_at.assert_not_called()

    def test_refuses_links_and_special_objects(self):
        target = self.root / "report"
        target.touch()
        nested = self.root / "actual"
        nested.mkdir()
        (nested / "report").touch()
        middle = self.root / "middle"
        middle.symlink_to(nested, target_is_directory=True)
        link = self.root / "link"
        link.symlink_to(target)
        self.assert_refused(middle / "report")
        self.assert_refused(link)

        hard = self.root / "hard"
        os.link(target, hard)
        self.assert_refused(hard)
        self.assert_refused(target)

        fifo = self.root / "queue"
        os.mkfifo(fifo)
        self.assert_refused(fifo)


class HostSharePathBoundaryTests(unittest.TestCase):
    def test_refuses_a_file_renamed_during_permission_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "report"
            moved = root / "moved"
            target.touch()
            policy = HostSharePolicy((str(root),))
            descriptor_seen = []

            def rename_after_open(proc_path, mode, *, effective_ids):
                self.assertEqual(mode, os.R_OK)
                self.assertTrue(effective_ids)
                descriptor_seen.append(int(proc_path.rsplit("/", 1)[-1]))
                target.rename(moved)
                target.touch()
                return True

            with mock.patch.object(host_share_paths.os, "access", side_effect=rename_after_open):
                with self.assertRaises(UnifierError):
                    with open_host_share_path(policy, str(target)):
                        self.fail("a replaced path passed verification")
            self.assertEqual(len(descriptor_seen), 1)
            self.assertTrue(moved.is_file())
            with self.assertRaises(OSError) as error:
                os.fstat(descriptor_seen[0])
            self.assertEqual(error.exception.errno, errno.EBADF)

    def test_refuses_a_mount_crossing_without_retrying_a_weaker_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = HostSharePolicy((str(root),))
            open_at = host_share_paths._openat2
            root_fds = []

            def simulate_boundary(directory, path, flags, resolve):
                if not root_fds:
                    descriptor = open_at(directory, path, flags, resolve)
                    root_fds.append(descriptor)
                    return descriptor
                self.assertEqual(directory, root_fds[0])
                self.assertEqual(path, "other-mount/report")
                self.assertTrue(resolve & host_share_paths._RESOLVE_NO_XDEV)
                raise OSError(errno.EXDEV, "simulated mount boundary")

            with mock.patch.object(host_share_paths, "_openat2", side_effect=simulate_boundary) as opened:
                with self.assertRaises(UnifierError):
                    with open_host_share_path(policy, str(root / "other-mount/report")):
                        self.fail("a cross-mount path passed verification")
            self.assertEqual(opened.call_count, 2)
            with self.assertRaises(OSError) as error:
                os.fstat(root_fds[0])
            self.assertEqual(error.exception.errno, errno.EBADF)

    def test_requires_host_permission_even_when_policy_allows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = HostSharePolicy((str(root),), writable=True)
            with mock.patch.object(host_share_paths.os, "access", return_value=False) as access:
                with self.assertRaises(UnifierError):
                    with open_host_share_path(policy, str(root), write=True):
                        self.fail("a host access denial was ignored")
            access.assert_called_once()
            args, kwargs = access.call_args
            self.assertTrue(args[0].startswith("/proc/self/fd/"))
            self.assertEqual(args[1], os.R_OK | os.W_OK | os.X_OK)
            self.assertEqual(kwargs, {"effective_ids": True})

    def test_refuses_symlink_in_an_approved_roots_ancestors(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            actual = parent / "actual"
            (actual / "nested").mkdir(parents=True)
            alias = parent / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            root = alias / "nested"
            policy = HostSharePolicy((str(root),))
            with self.assertRaises(UnifierError):
                with open_host_share_path(policy, str(root)):
                    self.fail("a symlink ancestor passed verification")
