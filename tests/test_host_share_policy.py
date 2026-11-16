"""Offline tests for decisions over trusted host paths."""

from dataclasses import FrozenInstanceError
import unittest

from unifier.errors import UnifierError
from unifier.host_share_policy import HostSharePolicy


class HostSharePolicyTests(unittest.TestCase):
    def test_scope_and_mode(self):
        roots = ("/home/user", "/media/user/data")
        read_only = HostSharePolicy(roots)
        writable = HostSharePolicy(roots, writable=True)
        for path in ("/home/user", "/home/user/文档/a b", "/media/user/data/file"):
            with self.subTest(path=path):
                self.assertTrue(read_only.allows(path))
                self.assertFalse(read_only.allows(path, write=True))
                self.assertTrue(writable.allows(path, write=True))
        for path in ("/home", "/home/user2", "/media/user/database", "/etc"):
            with self.subTest(path=path):
                self.assertFalse(writable.allows(path))
                self.assertFalse(writable.allows(path, write=True))
        self.assertFalse(HostSharePolicy().allows("/home/user"))

    def test_blacklist_denies_subtree_without_hiding_siblings(self):
        policy = HostSharePolicy(("/home/user",), blacklist=("/home/user/.ssh",),
                                 writable=True)
        self.assertTrue(policy.allows("/home/user"))
        self.assertTrue(policy.allows("/home/user/.ssh-notes/file", write=True))
        for path in ("/home/user/.ssh", "/home/user/.ssh/sub/file"):
            with self.subTest(path=path):
                self.assertFalse(policy.allows(path))
                self.assertFalse(policy.allows(path, write=True))

    def test_nested_admin_roots_each_require_an_explicit_open(self):
        parent = "/data/admin"
        child = "/data/admin/internal"
        admin = (parent, child)
        for opened, parent_ok, child_ok in (
                ((), False, False),
                ((parent,), True, False),
                ((child,), False, False),
                (admin, True, True)):
            with self.subTest(opened=opened):
                policy = HostSharePolicy(("/data",), admin_roots=admin,
                                         opened_admin_roots=opened, writable=True)
                self.assertEqual(policy.allows(parent + "/file", write=True), parent_ok)
                self.assertEqual(policy.allows(child + "/file"), child_ok)
                self.assertTrue(policy.allows("/data/admin-notes/file"))

    def test_admin_open_does_not_override_blacklist_or_scope(self):
        admin = ("/data/admin", "/outside")
        policy = HostSharePolicy(
            ("/data",), blacklist=("/data/admin/private",), admin_roots=admin,
            opened_admin_roots=admin, writable=True)
        self.assertTrue(policy.allows("/data/admin/file", write=True))
        self.assertFalse(policy.allows("/data/admin/private/file", write=True))
        self.assertFalse(policy.allows("/outside/file", write=True))

    def test_invalid_paths_fail_closed(self):
        policy = HostSharePolicy(("/",), writable=True)
        self.assertTrue(policy.allows("/", write=True))
        for path in ("", ".", "data", "//data", "/data//file", "/data/./file",
                     "/data/../file", "/data/", "/da\x00ta", None, b"/data"):
            with self.subTest(path=path):
                self.assertFalse(policy.allows(path))
                with self.assertRaises(UnifierError):
                    HostSharePolicy((path,))

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(UnifierError):
            HostSharePolicy("/data")
        with self.assertRaises(UnifierError):
            HostSharePolicy(("/data",), opened_admin_roots=("/data",))
        with self.assertRaises(TypeError):
            HostSharePolicy(("/data",), writable=1)
        with self.assertRaises(TypeError):
            HostSharePolicy(("/data",)).allows("/data", write=1)

    def test_configuration_is_immutable(self):
        roots = ["/data"]
        blocked = ["/data/private"]
        policy = HostSharePolicy(roots, blacklist=blocked)
        roots.append("/other")
        blocked.clear()
        self.assertFalse(policy.allows("/other/file"))
        self.assertFalse(policy.allows("/data/private/file"))
        with self.assertRaises(FrozenInstanceError):
            policy.writable = True


if __name__ == "__main__":
    unittest.main()
