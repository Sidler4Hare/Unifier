"""Exact cleanup identities and refusal before dangerous runtime operations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import reuse_test_guard as guard


class SupervisorContracts(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.out=Path(tmp.name)
        self.config={'cid':'a'*64,'project':str(self.out),'env_id':'test',
                     'container_name':'fixed','environment_uuid':'uuid','generation':1,
                     'image_lock':str(self.out/'images.json')}
        self.identity={'pid':123,'starttime':'999','boot_id':'boot','cgroup':'/aggregate/guest'}
        self.scope={'Id':'libpod-'+'a'*64+'.scope','InvocationID':'owned',
                    'ControlGroup':'/aggregate/guest'}
        self.binding={'cid':'a'*64,'started_at':'original','init':self.identity,'scope':self.scope}
        self.running={'State':{'Running':True,'StartedAt':'original','Pid':123}}
        guard.put(self.out/'images.json',{})

    def test_aborted_attempt_cannot_start_a_fresh_product_worker(self):
        guard.put(self.out/'abort.json',{'reason':'low-memory'})
        with self.assertRaisesRegex(RuntimeError,'latched'):
            guard.verify_permission(self.config,self.out)

    def test_zero_container_budget_and_wrong_cid_refuse_before_native_calls(self):
        guard.put(self.out/'spec.json',self.config)
        with patch('unifier.project.load_project',return_value=({}, {},self.out)), \
             patch('unifier.runtime._run_podman') as native, \
             patch.object(guard,'verify_permission'):
            service=guard.make_service(self.out/'spec.json')
            for command in (['podman','create','anything'],['podman','rm','a'*64],
                            ['podman','container','create','anything'],
                            ['podman','start','foreign']):
                with self.subTest(command=command),self.assertRaises(RuntimeError):
                    service.runtime.runner(command,capture=True,timeout=1)
            with self.assertRaisesRegex(RuntimeError,'zero new'):
                service.runtime.create(None)
            native.assert_not_called()

    def test_replaced_container_execution_is_never_stopped(self):
        guard.put(self.out/'live-binding.json',self.binding)
        other={'State':{'Running':True,'StartedAt':'replacement','Pid':123}}
        with patch.object(guard,'info',return_value=other),patch.object(guard,'call') as native:
            with self.assertRaisesRegex(RuntimeError,'CLEANUP_UNKNOWN'):
                guard.cleanup(self.config,self.out)
            native.assert_not_called()

    def test_exact_running_execution_is_stopped_and_verified(self):
        guard.put(self.out/'live-binding.json',self.binding)
        stopped={'State':{'Running':False,'StartedAt':'original','Pid':0}}
        with patch.object(guard,'info',side_effect=[self.running,stopped]), \
             patch.object(guard,'process',side_effect=[self.identity,FileNotFoundError()]), \
             patch.object(guard,'call') as native:
            result=guard.cleanup(self.config,self.out)
        self.assertEqual(result['status'],'STOPPED')
        native.assert_called_once_with(['podman','stop','--time','8','a'*64],timeout=25)

    def test_dead_proxy_does_not_prove_guest_exit_exact_scope_is_cleaned(self):
        guard.put(self.out/'live-binding.json',self.binding)
        stopped={'State':{'Running':False,'StartedAt':'original','Pid':0}}
        with patch.object(guard,'info',return_value=stopped), \
             patch.object(guard,'process',side_effect=[self.identity,FileNotFoundError()]), \
             patch.object(guard,'unit',return_value=self.scope),patch.object(guard,'call') as native:
            self.assertEqual(guard.cleanup(self.config,self.out)['status'],'STOPPED')
        native.assert_called_once_with(['systemctl','--user','stop',self.scope['Id']],timeout=15)

    def test_changed_scope_is_not_stopped_even_if_proxy_is_dead(self):
        guard.put(self.out/'live-binding.json',self.binding)
        stopped={'State':{'Running':False,'StartedAt':'original','Pid':0}}
        changed={**self.scope,'InvocationID':'replacement'}
        with patch.object(guard,'info',return_value=stopped), \
             patch.object(guard,'process',return_value=self.identity), \
             patch.object(guard,'unit',return_value=changed),patch.object(guard,'call') as native:
            with self.assertRaisesRegex(RuntimeError,'scope changed'):
                guard.cleanup(self.config,self.out)
            native.assert_not_called()

    def test_interrupted_start_without_guest_binding_preserves_unknown(self):
        guard.put(self.out/'start-intent.json',{'registered':True})
        with patch.object(guard,'info',return_value=self.running),patch.object(guard,'call') as native:
            with self.assertRaisesRegex(RuntimeError,'before execution binding'):
                guard.cleanup(self.config,self.out)
            native.assert_not_called()

    def test_controller_unit_replacement_is_not_stopped(self):
        original={'Id':'owned.service','InvocationID':'original','ControlGroup':'/aggregate/owned'}
        guard.put(self.out/'worker-unit.json',original)
        with patch.object(guard,'unit',return_value={**original,'InvocationID':'foreign'}), \
             patch.object(guard,'call') as native:
            with self.assertRaisesRegex(RuntimeError,'controller unit changed'):
                guard.cleanup(self.config,self.out)
            native.assert_not_called()

    def test_registered_cleanup_binding_is_saved_before_coverage_refusal(self):
        guard.put(self.out/'ready.json',{'aggregate':'/aggregate'})
        value={'State':{'Running':True,'StartedAt':'original','Pid':123},'ConmonPid':456}
        with patch.object(guard,'info',return_value=value), \
             patch.object(guard,'process',return_value={**self.identity,'cgroup':'/outside'}), \
             patch.object(guard,'conmon_process',return_value={**self.identity,'cgroup':'/outside'}), \
             patch.object(guard,'unit',return_value=self.scope):
            with self.assertRaisesRegex(RuntimeError,'escaped'):
                guard.capture_binding(self.config,self.out)
        self.assertFalse(guard.get(self.out/'live-binding.json')['coverage_verified'])

    def test_conmon_pidfile_is_bounded_and_never_follows_links(self):
        path=self.out/'conmon.pid';path.write_text('123\n')
        self.assertEqual(guard.read_pidfile(path),123)
        link=self.out/'alias';link.symlink_to(path)
        with self.assertRaises(OSError):guard.read_pidfile(link)
        for text in ('0','not-a-pid','1'*33):
            path.write_text(text)
            with self.subTest(text=text),self.assertRaises(RuntimeError):guard.read_pidfile(path)

    def test_missing_conmon_field_keeps_init_stop_binding(self):
        guard.put(self.out/'ready.json',{'aggregate':'/aggregate'})
        value={'State':{'Running':True,'StartedAt':'original','Pid':123}}
        with patch.object(guard,'info',return_value=value), \
             patch.object(guard,'process',return_value=self.identity), \
             patch.object(guard,'conmon_process',side_effect=RuntimeError('no conmon identity')), \
             patch.object(guard,'unit',return_value=self.scope):
            with self.assertRaisesRegex(RuntimeError,'no conmon identity'):
                guard.capture_binding(self.config,self.out)
        self.assertEqual(guard.get(self.out/'live-binding.json')['init'],self.identity)


if __name__=='__main__':unittest.main()
