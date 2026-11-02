"""The old package name remains an import path onto the unifier modules."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import unifier.errors
import multilinux.errors
from multilinux.errors import MultilinuxError, UnifierError


class PackageAliasTests(unittest.TestCase):
    def test_old_import_is_the_same_module(self):
        self.assertIs(multilinux.errors, unifier.errors)
        self.assertIs(MultilinuxError, UnifierError)

    def test_old_module_entry_points_at_unifier(self):
        from multilinux.cli import main as old_main
        from unifier.cli import main as new_main
        self.assertIs(old_main, new_main)


if __name__ == "__main__":
    unittest.main()
