"""Contracts for the repository checks and archived probe replay."""

import ast
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.repo_contract import load_contract, verify_entrypoints, verify_import_directions
from tools.run_archived_probe import archived_script, replay


class RepositoryContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contract = load_contract(Path(__file__).resolve().parents[1])
        paths = set()
        for name in self.contract["legacy_aliases"]["modules"]:
            paths.update({f"src/unifier/{name}.py", f"src/multilinux/{name}.py"})
        for entry in self.contract["entrypoints"]:
            paths.update(entry["files"] + entry["callers"] + entry["tests"])
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def test_missing_legacy_alias_is_detected_even_when_replacement_remains(self):
        (self.root / "src/multilinux/runtime.py").unlink()
        with self.assertRaisesRegex(ValueError, r"legacy alias multilinux\.runtime requires src/multilinux/runtime\.py"):
            verify_entrypoints(self.root, self.contract)

    def test_missing_guest_supervisor_names_affected_entry_point(self):
        (self.root / "src/unifier/application_supervisor.py").unlink()
        with self.assertRaisesRegex(ValueError, r"guest application supervisor .*requires .*application_supervisor\.py"):
            verify_entrypoints(self.root, self.contract)

    def test_wps_assets_may_be_omitted_from_source_but_partial_payload_is_rejected(self):
        verify_entrypoints(self.root, self.contract)
        path = self.root / "src/unifier/wps_portal_assets/manifest.json"
        path.parent.mkdir()
        path.touch()
        with self.assertRaisesRegex(ValueError, r"WPS file chooser .*incomplete candidate inputs: .*libqxdgdesktopportal\.so"):
            verify_entrypoints(self.root, self.contract)

    def test_import_baseline_permits_removing_debt_and_rejects_new_imports_and_cycles(self):
        trees = {
            "unifier.worker": ast.parse("from .internal import helper"),
            "unifier.internal.helper": ast.parse("pass"),
        }
        verify_import_directions(trees, self.contract)
        trees["unifier.internal.helper"] = ast.parse(
            "from .. import worker\nfrom tools import lab\nimport multilinux.runtime")
        with self.assertRaises(ValueError) as error:
            verify_import_directions(trees, self.contract)
        self.assertIn("new product import: unifier.internal.helper -> tools.lab", str(error.exception))
        self.assertIn("new product import: unifier.internal.helper -> multilinux.runtime", str(error.exception))
        self.assertIn("new cycle edge: unifier.worker -> unifier.internal.helper", str(error.exception))


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
