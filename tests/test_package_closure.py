"""Three-driver transaction/recovery matrix using fake native commands only."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from test_packages import PackageBoxes, make_env, package_service, assert_native_safe
from unifier.errors import UnifierError
from unifier.native_io import NativeCommandError
from unifier.packages import inventory_argv


class MatrixBoxes(PackageBoxes):
    interrupt = False

    def _add(self, env_id, names):
        return super()._add(env_id, ['local-demo' if name.startswith('/') else name for name in names])

    def package_exec(self, name, argv, **options):
        result = self.exec(name, argv, project=options['project'], env_id=options['env_id'],
                           workdir=None, env=options['env'], capture=True, timeout=options['timeout'])
        if self.interrupt and 'broken' in argv:
            self.interrupt = False
            raise NativeCommandError('fixture interrupted after write', stdout='partial write\n',
                                     returncode=-15, cleanup='unknown')
        return result


class PackageClosure(unittest.TestCase):
    def test_install_update_local_remove_failure_and_recovery_for_each_target(self):
        for target, suffix in [('debian13','.deb'),('fedora44','.rpm'),('arch','.pkg.tar.zst')]:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                runtime = MatrixBoxes()
                service, _ = package_service(root, runtime)
                make_env(service, runtime, target, 'box')
                peer_target = 'arch' if target != 'arch' else 'debian13'
                peer = make_env(service, runtime, peer_target, 'peer')
                runtime.events.clear()
                transactions = [service.install_packages('box', ['hello']), service.update_packages('box')]
                local = root/('local-demo'+suffix)
                local.write_bytes(b'offline fake package; never passed to a native package manager')
                transactions.append(service.install_local('box', str(local), package_name='local-demo'))
                transactions.append(service.remove_packages('box', ['hello','local-demo']))
                for txn in transactions:
                    assert_native_safe(self, txn)
                    self.assertEqual(txn['phase'], 'completed', txn)
                    self.assertFalse(txn['recovery_required'])
                    self.assertTrue(txn['other_environments']['peer']['unchanged'])
                self.assertEqual(service.store.load('peer')['package_inventory_sha256'],peer['package_inventory_sha256'])
                self.assertFalse(any(event[0] in ('start','exec') and event[1] == peer['runtime']['container_name']
                                     for event in runtime.events))
                failure = service.install_packages('box', ['missing-fixture'])
                self.assertEqual(failure['phase'], 'failed')
                self.assertNotIn('missing-fixture', runtime.installed['box'])
                runtime.interrupt = True
                interrupted = service.install_packages('box', ['broken'])
                self.assertEqual(interrupted['phase'], 'needs-recovery')
                with self.assertRaisesRegex(UnifierError, 'pkg recover'):
                    service.install_packages('box', ['next'])
                service.stop('box')
                service.start('box')
                if target == 'arch':
                    # A clean database audit cannot prove interrupted pacman hooks finished.
                    with self.assertRaisesRegex(UnifierError, 'post-transaction completion'):
                        service.recover_packages('box')
                    self.assertEqual(service.show_transaction(interrupted['id'])['phase'], 'needs-recovery')
                    self.assertTrue(service.store.load('box')['package_recovery_required'])
                    service.stop('box')
                    continue
                self.assertEqual(service.recover_packages('box')['status'], 'recovered')
                actual = runtime._inventory('box', inventory_argv(target))
                self.assertEqual(service.store.load('box')['package_inventory_sha256'],
                                 hashlib.sha256(actual.encode()).hexdigest())
                self.assertEqual(service.show_transaction(interrupted['id'])['phase'], 'failed')
                self.assertEqual(service.install_packages('box', ['next'])['phase'], 'completed')
