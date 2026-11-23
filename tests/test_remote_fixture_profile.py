"""Remote fixtures keep their existing smaller limits and exact identities."""
import json
import unittest
from unittest.mock import patch
from tools import reuse_test_guard as guard


class RemoteFixtureProfile(unittest.TestCase):
    def config(self, target='fedora44'):
        cid, uuid = guard.REMOTE_PACKAGE_BINDINGS[target]
        return dict(fixture_profile='remote-package-20261112', cid=cid,
                    environment_uuid=uuid, generation=1, env_id=target,
                    project='/home/agent/expansion-continuation-20261103/bridge-environments',
                    container_name='ml-2b804374-'+target, seconds=300,
                    shared='/home/agent/expansion-continuation-20261103/bridge-shared')

    def test_changed_identity_or_budget_profile_refuses_before_podman(self):
        for change in ({'cid':'a'*64}, {'environment_uuid':'foreign'}, {'generation':2},
                       {'generation':True}, {'env_id':'debian13'}, {'project':'/other'},
                       {'container_name':'other'}, {'seconds':301}, {'seconds':True},
                       {'fixture_profile':'unknown'}, {'task_profile':'sh10-20261112-close-fixed'}):
            with self.subTest(change=change), patch.object(guard,'call') as native:
                with self.assertRaisesRegex(RuntimeError,'not bound'):
                    guard.info({**self.config(), **change})
                native.assert_not_called()

    def test_both_stopped_instances_require_their_actual_smaller_limits(self):
        for target in guard.REMOTE_PACKAGE_BINDINGS:
            c=self.config(target)
            record={'environment_uuid':c['environment_uuid'],'generation':1,
                    'runtime':{'container_id':c['cid']}}
            value={'Id':c['cid'], 'Config':{'Labels':{'multilinux.project':c['project'],
                   'multilinux.env':target,'multilinux.role':'environment'},'Env':[]},
                   'State':{'Running':False,'Pid':0},
                   'HostConfig':{'CgroupParent':'unifier-bridge-tests.slice',
                       'Memory':384*guard.MIB,'MemorySwap':512*guard.MIB,'PidsLimit':128,
                       'NanoCpus':1000000000,'IpcMode':'private','PidMode':'private',
                       'RestartPolicy':{'Name':'no'}},
                   'Mounts':[{'Source':c['shared'],'Destination':c['shared']}]}
            with patch.object(guard,'get',return_value=record), \
                 patch.object(guard,'configured_resources',return_value={'pids':{'limit':128}}):
                with patch.object(guard,'call',return_value=json.dumps([value])):
                    self.assertEqual(guard.info(c),value)
                for field, changed in [('Memory',768*guard.MIB),('PidsLimit',192),
                                       ('CgroupParent','user.slice'),('IpcMode','host')]:
                    invalid={**value,'HostConfig':{**value['HostConfig'],field:changed}}
                    with self.subTest(target=target,field=field), \
                         patch.object(guard,'call',return_value=json.dumps([invalid])):
                        with self.assertRaises(RuntimeError):
                            guard.info(c)
            self.assertEqual(guard.task_budget(c),{'aggregate':256,'guest':128})

    def test_longer_package_preparation_keeps_both_fixed_bindings(self):
        for target in guard.REMOTE_PACKAGE_BINDINGS:
            spec = {**self.config(target), 'fixture_profile': 'remote-package-prepare', 'seconds': 900}
            self.assertEqual(guard.fixture_resources(spec), guard.fixture_resources(self.config(target)))
            self.assertEqual(guard.task_budget(spec), {'aggregate': 256, 'guest': 128})
            for changed in ({'seconds': 901}, {'seconds': 0}, {'seconds': True},
                            {'cid': '0'*64}, {'generation': 2}, {'task_profile': 'debian-private-gui'}):
                with self.subTest(target=target, changed=changed), patch.object(guard, 'call') as podman:
                    with self.assertRaises(RuntimeError):
                        guard.info({**spec, **changed})
                    podman.assert_not_called()
