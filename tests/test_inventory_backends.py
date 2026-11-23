"""Actual runtime/helper dispatch across three DBs, with all Podman calls faked."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_packages import make_env, package_service
from test_package_inventory import stanza
from test_pacman_inventory import desc
from test_rpm_inventory import create_rpm_database, rpm
from unifier.inventory_probe import probe, inspect_stopped
from unifier.runtime import PodmanRuntime
from unifier.view_journal import write_journal
from unifier.errors import UnifierError


class BackendDispatch(unittest.TestCase):
    def test_actual_runtime_and_storage_probe_dispatch_without_guest_execution(self):
        for target, database in [('debian13', 'dpkg'), ('arch', 'pacman'), ('fedora44', 'rpm-sqlite')]:
            if target == 'fedora44' and rpm is None:
                continue  # Native RPM coverage is separately reported as skipped.
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                service, fake = package_service(root)
                record = make_env(service, fake, target, 'peer')
                record['runtime']['container_id'] = 'a'*64
                merged = root/'merged'
                if target == 'debian13':
                    db = merged/'var/lib/dpkg'
                    (db/'updates').mkdir(parents=True)
                    (db/'lock').touch()
                    (db/'status').write_text(stanza())
                elif target == 'arch':
                    db = merged/'var/lib/pacman/local'
                    (db/'base-1').mkdir(parents=True)
                    (db/'base-1/desc').write_text(desc())
                    (db/'ALPM_DB_VERSION').write_text('9\n')
                else:
                    create_rpm_database(merged)
                calls = []
                def storage(argv, **options):
                    calls.append(argv)
                    command = argv[1]
                    self.assertEqual(argv[-1], 'a'*64)
                    if command == 'inspect':
                        data = {'Id':'a'*64, 'Config':{'Labels':{
                            'multilinux.project':record['runtime']['project'],
                            'multilinux.env':'peer', 'multilinux.role':'environment'}},
                            'State':{'Running':False, 'Status':'exited'}, 'Mounts':[]}
                        return subprocess.CompletedProcess(argv, 0, json.dumps([data]), '')
                    self.assertIn(command, ('mount', 'umount'))
                    return subprocess.CompletedProcess(argv, 0, str(merged) if command == 'mount' else '', '')
                def helper(argv, **options):
                    self.assertEqual(argv[:2], ['podman', 'unshare'])
                    supplied = json.loads(argv[argv.index('--record')+1])
                    result = probe(supplied, Path(argv[argv.index('--journal')+1]),
                                   argv[argv.index('--token')+1], run=storage)
                    return subprocess.CompletedProcess(argv, 0, json.dumps(asdict(result)), '')
                runtime = PodmanRuntime(runner=helper, check_rootless=False)
                info = replace(fake.inspect(record['runtime']['container_name']), container_id='a'*64,
                               status='exited', running=False)
                with patch.object(runtime, '_require_owned', return_value=info):
                    result = runtime.read_inventory_readonly(record)
                self.assertEqual(result.status, 'supported', result.reason)
                self.assertEqual(result.database, database)
                self.assertEqual(result.binding['container_id'], 'a'*64)
                self.assertEqual([c[1] for c in calls], ['inspect','mount','inspect','inspect','umount'])

    def test_every_backend_rejects_shadowed_ancestor_and_descendant_before_mount(self):
        from unifier.inventory_backends import BACKENDS
        for _, paths, _ in BACKENDS.values():
            for path in paths:
                for destination in (path, str(Path(path).parent), path+'/lock'):
                    data = {'Id':'a'*64, 'Config':{'Labels':{'multilinux.project':'root',
                            'multilinux.env':'peer','multilinux.role':'environment'}},
                            'State':{'Running':False,'Status':'exited'},
                            'Mounts':[{'Destination':destination}]}
                    def runner(argv, **options):
                        self.assertEqual(argv[1], 'inspect')
                        return subprocess.CompletedProcess(argv, 0, json.dumps([data]), '')
                    with self.subTest(destination=destination), self.assertRaisesRegex(UnifierError, 'mounted'):
                        inspect_stopped('a'*64, 'root', 'peer', run=runner, database_paths=paths)

    def test_running_peer_remains_unsupported_without_storage_acquisition(self):
        with tempfile.TemporaryDirectory() as temp:
            service, fake = package_service(Path(temp))
            record = make_env(service, fake, 'arch', 'peer')
            info = replace(fake.inspect(record['runtime']['container_name']), running=True, status='running')
            def forbidden(*args, **kwargs):
                raise AssertionError('running peer must not reach the storage helper')
            runtime = PodmanRuntime(runner=forbidden, check_rootless=False)
            with patch.object(runtime, '_require_owned', return_value=info):
                value = runtime.read_inventory_readonly(record)
            self.assertEqual(value.status, 'unsupported')
            self.assertEqual(value.database, 'pacman')
