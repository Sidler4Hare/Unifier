"""Packing reproducibility, source identity and failure publication boundary."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import build_release
from tools.check_repo import verify_bundle


class ReleaseDocumentation(unittest.TestCase):
    def test_current_release_links_resolve_in_staged_source_and_bundles(self):
        root = Path(__file__).resolve().parents[1]
        documents = (
            'README.md', 'README.zh-CN.md', 'docs/当前状态.md', 'docs/v0.0.6.md',
            'docs/v0.0.7.md', 'docs/三目标包管理收口-20261112.md',
        )
        for name in documents:
            document = root / name
            for target in re.findall(r'\]\(([^)\n]+)\)', document.read_text(encoding='utf-8')):
                if '://' in target:
                    continue
                with self.subTest(document=name, target=target):
                    relative = target.partition('#')[0]
                    path = (document.parent / relative).resolve() if relative else document
                    self.assertTrue(path.is_relative_to(root) and path.is_file())


class ReleaseProvenance(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.root = self.work / 'source'
        self.root.mkdir()
        for name in ('src', 'tools', 'tests', 'config', 'fixtures', 'bin', 'docs', '设计'):
            (self.root/name).mkdir()
        for name in build_release.SOURCE_FILES:
            (self.root/name).write_text('fixture\n')
        (self.root/'bin/tool').write_text('hello\n')
        self.addCleanup(patch.stopall)
        patch.object(build_release, 'ROOT', self.root).start()
        patch.object(build_release, '__version__', '1.2.3.dev0').start()
        # Only replace the expensive checker in miniature packing fixtures.
        # The real candidate is independently checked by the build workflow.
        self.check = patch.object(build_release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)).start()

    def pack(self, name):
        return build_release.build(self.work/name)

    def test_same_snapshot_is_byte_reproducible_and_records_baseline(self):
        base = {'version': '0.0.6', 'package_sha256': 'a'*64, 'manifest_sha256': 'b'*64}
        (self.root/'config/release-baseline.json').write_text(json.dumps(base))
        a, b = self.pack('a.tar.gz'), self.pack('b.tar.gz')
        self.assertEqual(Path(a['path']).read_bytes(), Path(b['path']).read_bytes())
        self.assertEqual(a['base_release'], base)
        self.assertIsNone(a['source_revision'])
        with tarfile.open(a['path']) as archive:
            archive.extractall(self.work/'unpacked', filter='data')
        extracted = self.work/'unpacked/unifier-1.2.3.dev0'
        self.assertEqual((extracted/'README.zh-CN.md').read_bytes(),
                         (self.root/'README.zh-CN.md').read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            verify_bundle(extracted)
        (extracted/'bin/tool').chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'mode differs'):
            verify_bundle(extracted)

    def test_content_and_permissions_change_identity_without_a_baseline(self):
        first = self.pack('a.tar.gz')
        self.assertIsNone(first['base_release'])
        (self.root/'bin/tool').write_text('changed\n')
        second = self.pack('b.tar.gz')
        (self.root/'bin/tool').chmod(0o755)
        third = self.pack('c.tar.gz')
        self.assertEqual(len({x['source_tree_sha256'] for x in (first, second, third)}), 3)

    def test_failed_independent_check_never_publishes_archive(self):
        self.check.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaises(subprocess.CalledProcessError):
            self.pack('failed.tar.gz')
        self.assertFalse((self.work/'failed.tar.gz').exists())
        self.assertTrue((self.work/'failed.tar.gz.validation.log').exists())

    def test_modified_manifest_cannot_keep_old_source_digest(self):
        result = self.pack('a.tar.gz')
        with tarfile.open(result['path']) as archive:
            archive.extractall(self.work/'unpacked', filter='data')
        root = self.work/'unpacked/unifier-1.2.3.dev0'
        path = root/'BUNDLE-MANIFEST.json'
        data = json.loads(path.read_text())
        del data['files']['README.md']
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'tree digest differs'):
            verify_bundle(root)

    def candidate_fixture(self):
        inputs = self.work / "candidate"
        inputs.mkdir()
        declarations = {}
        for name, mode in zip(build_release.WPS_FILES, (0o644, 0o600, 0o755, 0o700)):
            path = inputs / name
            path.parent.mkdir(exist_ok=True)
            payload = f"fixture for {name}\n".encode()
            path.write_bytes(payload)
            path.chmod(mode)
            relative = f"{build_release.WPS_DESTINATION}/{name}"
            declarations[relative] = {
                "kind": "file", "mode": mode, "sha256": hashlib.sha256(payload).hexdigest(),
            }
        manifest = self.work / "frozen.manifest.json"
        manifest.write_text(json.dumps({"version": build_release.WPS_SOURCE_VERSION,
                                        "files": declarations}))
        patch.object(build_release, "WPS_SOURCE_MANIFEST_SHA256",
                     hashlib.sha256(manifest.read_bytes()).hexdigest()).start()
        init = self.root / "src/unifier/__init__.py"
        init.parent.mkdir()
        init.write_text('__version__ = "1.2.3.dev0"\n')
        return inputs, manifest, declarations

    def test_candidate_records_frozen_assets_and_clean_snapshot(self):
        inputs, source_manifest, declarations = self.candidate_fixture()
        revision = "a" * 40

        def snapshot(destination, received_revision):
            self.assertEqual(received_revision, revision)
            shutil.copytree(self.root, destination)

        version = "1.2.3.dev0+refactor.1"
        options = {"candidate_version": version, "candidate_inputs": inputs,
                   "candidate_source_manifest": source_manifest}
        with (patch.object(build_release, "_candidate_revision", return_value=revision),
              patch.object(build_release, "_candidate_snapshot", side_effect=snapshot)):
            first = build_release.build(self.work / "candidate-a.tar.gz", **options)
            second = build_release.build(self.work / "candidate-b.tar.gz", **options)
            source_version = "1.2.3.dev0"
            plain = build_release.build(self.work / "candidate-source-version.tar.gz", **{
                **options, "candidate_version": source_version})
        self.assertEqual(Path(first["path"]).read_bytes(), Path(second["path"]).read_bytes())
        self.assertEqual(first["source_revision"], revision)
        self.assertEqual(first["wps_asset_source"]["files"], declarations)
        self.assertEqual(plain["wps_asset_source"], first["wps_asset_source"])
        with tarfile.open(plain["path"]) as archive:
            with archive.extractfile(f"unifier-{source_version}/src/unifier/__init__.py") as source:
                self.assertEqual(source.read(), (self.root / "src/unifier/__init__.py").read_bytes())
        self.assertEqual(first["wps_asset_source"]["manifest_sha256"],
                         hashlib.sha256(source_manifest.read_bytes()).hexdigest())
        self.assertNotEqual(first["source_tree_sha256"], first["source_snapshot_sha256"])
        with tarfile.open(first["path"]) as archive:
            archive.extractall(self.work / "candidate-unpacked", filter="data")
        extracted = self.work / "candidate-unpacked" / f"unifier-{version}"
        manifest = json.loads((extracted / "BUNDLE-MANIFEST.json").read_text())
        self.assertEqual(manifest["source_kind"], "git-candidate")
        self.assertEqual(manifest["wps_asset_source"], first["wps_asset_source"])
        self.assertEqual((extracted / "src/unifier/__init__.py").read_text(),
                         f'__version__ = "{version}"\n')
        self.assertEqual((self.root / "src/unifier/__init__.py").read_text(),
                         '__version__ = "1.2.3.dev0"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            verify_bundle(extracted)

    def test_candidate_rejects_extra_missing_modified_and_linked_assets(self):
        inputs, source_manifest, _declarations = self.candidate_fixture()
        extra = inputs / "unexpected"
        extra.touch()
        with self.assertRaisesRegex(ValueError, "input inventory"):
            build_release._candidate_assets(inputs, source_manifest)
        extra.unlink()
        license_path = inputs / "LICENSE.LGPL3"
        original = license_path.read_bytes()
        license_path.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "differs from the frozen desktop release"):
            build_release._candidate_assets(inputs, source_manifest)
        license_path.write_bytes(original)
        license_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "differs from the frozen desktop release"):
            build_release._candidate_assets(inputs, source_manifest)
        license_path.unlink()
        with self.assertRaisesRegex(ValueError, "input inventory"):
            build_release._candidate_assets(inputs, source_manifest)
        saved = self.work / "license"
        saved.write_bytes(original)
        saved.chmod(0o644)
        license_path.symlink_to(saved)
        with self.assertRaises(OSError):
            build_release._candidate_assets(inputs, source_manifest)

    def test_candidate_rejects_modified_source_manifest_and_dirty_checkout(self):
        inputs, source_manifest, _declarations = self.candidate_fixture()
        source_manifest.write_text("{}")
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            build_release._candidate_assets(inputs, source_manifest)
        with patch.object(build_release.subprocess, "check_output", side_effect=[
                str(self.root) + "\n", "a" * 40 + "\n", b" M bin/tool\n"]):
            with self.assertRaisesRegex(ValueError, "clean source checkout"):
                build_release._candidate_revision()

    def test_candidate_checks_extracted_assets_before_publication(self):
        inputs, source_manifest, _declarations = self.candidate_fixture()
        version = "1.2.3.dev0+refactor.1"
        output = self.work / "corrupt.tar.gz"
        extract = tarfile.TarFile.extractall

        def corrupt(archive, destination, **kwargs):
            extract(archive, destination, **kwargs)
            asset = Path(destination) / f"unifier-{version}" / build_release.WPS_DESTINATION
            (asset / "LICENSE.LGPL3").write_bytes(b"wrong asset")

        with (patch.object(build_release, "_candidate_revision", return_value="a" * 40),
              patch.object(build_release, "_candidate_snapshot",
                           side_effect=lambda destination, _revision: shutil.copytree(self.root, destination)),
              patch.object(tarfile.TarFile, "extractall", new=corrupt)):
            with self.assertRaisesRegex(ValueError, "extracted WPS asset differs"):
                build_release.build(output, candidate_version=version, candidate_inputs=inputs,
                                    candidate_source_manifest=source_manifest)
        self.assertFalse(output.exists())
        self.check.assert_not_called()
