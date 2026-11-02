"""Version switching preserves data and an executable rollback target."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import install_unifier
from tools.check_repo import verify_bundle


class ReleaseInstall(unittest.TestCase):
    def test_upgrade_requires_opt_in_and_preserves_relative_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bin").mkdir()
            old, new = root / "old", root / "new"
            launcher = (ROOT / "bin/unifier").read_bytes()
            old.write_bytes(launcher)
            new.write_bytes(launcher)
            destination = root / "bin/unifier"
            destination.symlink_to("../old")
            data = root / "userdata"
            data.write_bytes(b"retained data")
            with patch.object(install_unifier, "LAUNCHER", new), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(install_unifier.main(["--bin-dir", str(root / "bin")]), 1)
                self.assertEqual(os.readlink(destination), "../old")
                self.assertEqual(install_unifier.main(["--bin-dir", str(root / "bin"), "--upgrade", "--dry-run"]), 0)
                self.assertEqual(list((root / "bin").iterdir()), [destination])
                self.assertEqual(install_unifier.main(["--bin-dir", str(root / "bin"), "--upgrade"]), 0)
            self.assertEqual(destination.resolve(), new)
            backup, = (root / "bin").glob("*.rollback")
            self.assertEqual(os.readlink(backup), "../old")
            os.replace(backup, destination)
            self.assertEqual(destination.resolve(), old)
            self.assertEqual(data.read_bytes(), b"retained data")

    def test_upgrade_still_refuses_unknown_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "other"
            other.write_text("unrelated application")
            destination = root / "unifier"
            destination.symlink_to("other")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(install_unifier.main(["--bin-dir", str(root), "--upgrade"]), 1)
            self.assertEqual(os.readlink(destination), "other")

    def test_manifest_rejects_changed_file_and_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"original")
            link = root / "alias"
            link.symlink_to("source")
            manifest = {"files": {"source": {"kind": "file", "sha256": hashlib.sha256(b"original").hexdigest()},
                                  "alias": {"kind": "symlink", "target": "source"}}}
            (root / "BUNDLE-MANIFEST.json").write_text(json.dumps(manifest))
            with contextlib.redirect_stdout(io.StringIO()):
                verify_bundle(root)
            source.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                verify_bundle(root)
            source.write_bytes(b"original")
            link.unlink()
            link.symlink_to("elsewhere")
            with self.assertRaises(ValueError):
                verify_bundle(root)


if __name__ == "__main__":
    unittest.main()
