"""Packing reproducibility, source identity and failure publication boundary."""
import contextlib
import io
import json
from pathlib import Path
import re
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
            'README.md', 'docs/当前状态.md', 'docs/v0.0.6.md',
            'docs/v0.0.7.md', 'docs/三目标包管理收口-20261112.md',
        )
        for name in documents:
            document = root / name
            for target in re.findall(r'\]\(([^)\n]+)\)', document.read_text(encoding='utf-8')):
                if '://' in target:
                    continue
                with self.subTest(document=name, target=target):
                    path = (document.parent / target).resolve()
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
        for name in ('README.md', 'HANDOFF.md', 'AGENTS.md', '.gitignore', '.gitattributes'):
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
