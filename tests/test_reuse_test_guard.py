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

    def test_parent_swap_cap_is_accepted_without_changing_container_limits(self):
        guard.put(self.out/'ready.json',{'aggregate':'/aggregate'})
        value={'State':{'Running':True,'StartedAt':'original','Pid':123},
               'HostConfig':{'NetworkMode':'none'},'NetworkSettings':{}}
        init={**self.identity,'cgroup':'/aggregate/guest/container'}
        raw={
            '/aggregate/guest/container':{'memory.max':'max','memory.swap.max':'max','pids.max':'max','cpu.max':'max 100000'},
            '/aggregate/guest':{'memory.max':str(768*guard.MIB),'memory.swap.max':str(896*guard.MIB),'pids.max':'192','cpu.max':'100000 100000'},
            '/aggregate':{'memory.max':str(1024*guard.MIB),'memory.swap.max':str(128*guard.MIB),'pids.max':'256','cpu.max':'100000 100000'}}
        with patch.object(guard,'info',return_value=value),patch.object(guard,'process',return_value=init), \
             patch.object(guard,'conmon_process',return_value={**self.identity,'cgroup':'/aggregate/controller'}), \
             patch.object(guard,'unit',return_value=self.scope),patch.object(guard,'limits',side_effect=raw.__getitem__), \
             patch.object(guard,'aggregate_processes',return_value=[]),patch.object(guard.Path,'iterdir',return_value=iter(())):
            binding=guard.capture_binding(self.config,self.out)
        self.assertTrue(binding['coverage_verified'])
        self.assertEqual(binding['guest_limits']['memory.swap.max'],128*guard.MIB)
        self.assertEqual(len(binding['guest_limits']['chain']),3)


class EffectiveLimitContracts(unittest.TestCase):
    def test_caps_can_be_on_different_ancestors_and_cpu_periods(self):
        raw={
          '/job/guest/container':{'memory.max':'805306368','memory.swap.max':'max','pids.max':'max','cpu.max':'max 100000'},
          '/job/guest':{'memory.max':'max','memory.swap.max':'939524096','pids.max':'192','cpu.max':'50000 100000'},
          '/job':{'memory.max':'1073741824','memory.swap.max':'134217728','pids.max':'256','cpu.max':'200000 100000'}}
        with patch.object(guard,'limits',side_effect=raw.__getitem__):
            value=guard.effective_limits('/job/guest/container','/job')
        self.assertEqual(value['memory.max'],805306368)
        self.assertEqual(value['memory.swap.max'],134217728)
        self.assertEqual(value['pids.max'],192)
        self.assertEqual(value['cpu.max'],{'quota':50000,'period':100000})

    def test_unbounded_is_unknown_and_zero_swap_remains_zero(self):
        raw={'memory.max':'max','memory.swap.max':'0','pids.max':'max','cpu.max':'max 100000'}
        with patch.object(guard,'limits',return_value=raw):value=guard.effective_limits('/job','/job')
        self.assertIsNone(value['memory.max']);self.assertEqual(value['memory.swap.max'],0)
        self.assertIsNone(value['cpu.max'])

    def test_outside_or_traversing_chain_is_rejected_before_reading(self):
        for group,parent in (('/job-other/guest','/job'),('/job/../other','/job'),('relative','/job')):
            with self.subTest(group=group),patch.object(guard,'limits') as read:
                with self.assertRaises(RuntimeError):guard.effective_limits(group,parent)
                read.assert_not_called()

    def test_invalid_or_missing_bound_is_never_filled_with_zero(self):
        valid={'memory.max':'1','memory.swap.max':'1','pids.max':'1','cpu.max':'1 100000'}
        for key,value in (('memory.max','-1'),('pids.max','unknown'),('cpu.max','1 0'),('cpu.max','garbage')):
            with self.subTest(key=key,value=value),patch.object(guard,'limits',return_value={**valid,key:value}):
                with self.assertRaises(RuntimeError):guard.effective_limits('/job','/job')
        with patch.object(guard,'limits',return_value={}):
            with self.assertRaises(RuntimeError):guard.effective_limits('/job','/job')


class ControllerBudgetContracts(unittest.TestCase):
    def test_omitted_controller_stays_on_the_original_profile(self):
        budget=guard.controller_budget({})
        self.assertEqual(budget, guard.DEFAULT_CONTROLLER)
        budget['memory_max']=1
        self.assertEqual(guard.DEFAULT_CONTROLLER['memory_max'], 192*guard.MIB)
        props=guard.controller_properties(guard.controller_budget({}))
        self.assertIn('MemoryMax='+str(192*guard.MIB), props)
        self.assertIn('MemoryHigh='+str(144*guard.MIB), props)
        self.assertIn('TasksMax=64', props)

    def test_only_the_authorized_profile_may_raise_the_controller(self):
        spec={'controller':dict(guard.AUTHORIZED_CONTROLLER)}
        self.assertEqual(guard.controller_budget(spec), guard.AUTHORIZED_CONTROLLER)
        props=guard.controller_properties(guard.AUTHORIZED_CONTROLLER)
        self.assertIn('MemoryMax='+str(384*guard.MIB), props)
        self.assertIn('TasksMax=128', props)
        rejected=[
            {'controller':None},
            {'controller':{'memory_max':256*guard.MIB,'memory_high':192*guard.MIB,'tasks_max':96}},
            {'controller':{**guard.AUTHORIZED_CONTROLLER,'swap':0}},
            {'controller':{key:str(value) for key,value in guard.AUTHORIZED_CONTROLLER.items()}},
            {'controller':{**guard.AUTHORIZED_CONTROLLER,'memory_high':384*guard.MIB}},
        ]
        for item in rejected:
            with self.subTest(item=item), self.assertRaisesRegex(RuntimeError,'admitted profile'):
                guard.controller_budget(item)

    def test_actual_unit_limits_must_match_the_selected_profile(self):
        root=Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__('shutil').rmtree(root))
        group=root/'job'; group.mkdir()
        files={'memory.max':str(384*guard.MIB),'memory.high':str(288*guard.MIB),
               'memory.swap.max':'0','pids.max':'128',
               'memory.current':'10','pids.current':'1',
               'memory.stat':'anon 4\nfile 6\nkernel 1\n'}
        for name,text in files.items():
            (group/name).write_text(text)
        child=group/'child'; child.mkdir()
        for name,text in files.items():
            (child/name).write_text(text)
        with patch.object(guard,'CG',root):
            self.assertEqual(guard.assert_controller_limits('/job', guard.AUTHORIZED_CONTROLLER)['tasks_max'], 128)
            self.assertEqual(guard.memory_classes('/job'), {'anon':4,'file':6})
            self.assertEqual(guard.subgroup_metrics('/job')[0]['cgroup'], '/job/child')
            (group/'memory.max').write_text(str(192*guard.MIB))
            with self.assertRaisesRegex(RuntimeError,'limits differ'):
                guard.assert_controller_limits('/job', guard.AUTHORIZED_CONTROLLER)
            (group/'memory.stat').write_text('anon 1\n')
            with self.assertRaisesRegex(RuntimeError,'missing anon or file'):
                guard.memory_classes('/job')

    def test_too_many_subgroups_are_refused(self):
        root=Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__('shutil').rmtree(root))
        group=root/'job'; group.mkdir()
        for index in range(17):
            (group/f'c{index}').mkdir()
        with patch.object(guard,'CG',root), self.assertRaisesRegex(RuntimeError,'exceeds budget'):
            guard.subgroup_metrics('/job')


if __name__=='__main__':unittest.main()
