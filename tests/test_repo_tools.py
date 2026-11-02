"""Archive relocation must preserve driver bytes, paths and failure status."""

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.run_archived_probe import archived_script, replay


class ArchivedReplayTests(unittest.TestCase):
    def test_original_layout_dependencies_and_nonzero_exit_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repository"
            archive = root / "archive/evidence"
            (archive / "original").mkdir(parents=True)
            (archive / "dependency").mkdir()
            (root / "src").mkdir()
            (root / "src/marker").write_text("current source")
            (root / "evidence").symlink_to("archive/evidence")
            dependency = "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n"
            (archive / "dependency/helper.py").write_text(dependency)
            payload = '''from pathlib import Path
import os, sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'evidence/dependency'))
import helper
assert helper.ROOT == ROOT
assert (ROOT / 'src/marker').read_text() == 'current source'
assert Path(os.environ['UNIFIER_HOME']).parent == ROOT
assert sys.argv[1:] == ['--literal', 'two words']
(ROOT / 'result').write_text('reached driver')
raise SystemExit(7)
'''
            probe = archive / "original/probe.py"
            probe.write_text(payload)
            script = archived_script(root, "evidence/original/probe.py")
            workspace = base / "replay"
            with contextlib.redirect_stderr(io.StringIO()):
                code = replay(root, script, workspace, ["--literal", "two words"],
                              include_runs=["dependency"])
            self.assertEqual(code, 7)
            self.assertEqual((workspace / "result").read_text(), "reached driver")
            self.assertEqual(probe.read_text(), payload)
            self.assertEqual((workspace / "evidence/original/probe.py").read_text(), payload)
            self.assertFalse((root / "result").exists())
            self.assertFalse((root / ".unifier-home").exists())

    def test_existing_workspace_and_repository_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            (root / "archive/evidence/run").mkdir(parents=True)
            (root / "archive/evidence/run/probe.py").write_text("raise SystemExit(0)\n")
            script = archived_script(root, "archive/evidence/run/probe.py")
            with self.assertRaises(ValueError):
                replay(root, script, root / "src/replay", [])
            workspace = Path(temporary) / "existing"
            workspace.mkdir()
            marker = workspace / "keep"
            marker.write_text("keep")
            with self.assertRaises(FileExistsError):
                replay(root, script, workspace, [])
            self.assertEqual(marker.read_text(), "keep")

    def test_driver_outside_archive_and_invalid_dependency_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            (root / "archive/evidence/run").mkdir(parents=True)
            (root / "archive/evidence/run/probe.py").write_text("raise SystemExit(0)\n")
            (root / "outside.py").write_text("raise SystemExit(0)\n")
            with self.assertRaises(ValueError):
                archived_script(root, "outside.py")
            script = archived_script(root, "archive/evidence/run/probe.py")
            with self.assertRaises(ValueError):
                replay(root, script, Path(temporary) / "replay", [], include_runs=["../run"])


if __name__ == "__main__":
    unittest.main()
