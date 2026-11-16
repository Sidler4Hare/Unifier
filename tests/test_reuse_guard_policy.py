"""No real pressure is generated: inject measurements into stop decisions."""
from copy import deepcopy
import unittest
from tools.reuse_guard_policy import GIB, GuardPolicy, approved_mounts, same_execution


def sample(**changes):
    return dict(available=4*GIB, full=0., some=0., memory=100, tasks=1,
                oom_events=0, max_events=0, pids_events=0, **changes)


class PolicyTests(unittest.TestCase):
    def policy(self):
        return GuardPolicy(deadline=1000)

    def test_low_memory_requires_two_samples_and_recovers(self):
        p=self.policy(); low=sample();low['available']=GIB-1
        self.assertIsNone(p.check(low,now=1));self.assertIsNone(p.check(sample(),now=2))
        self.assertIsNone(p.check(low,now=3))
        self.assertEqual(p.check(low,now=3.25),'ABORTED_HOST_RESERVE')

    def test_memory_grace_resets_and_task_limit_is_unchanged(self):
        p=self.policy();x=sample();x['memory']=GIB*.95
        self.assertIsNone(p.check(x,now=0))
        self.assertIsNone(p.check(x,now=1.99))
        self.assertEqual(p.check(x,now=2),'ABORTED_MEMORY')
        p=self.policy();p.check(x,now=0)
        low=sample();low['memory']=GIB*.949
        self.assertIsNone(p.check(low,now=1))
        self.assertIsNone(p.check(x,now=2))
        self.assertIsNone(p.check(x,now=3.99))
        self.assertEqual(p.check(x,now=4),'ABORTED_MEMORY')
        p=self.policy();x=sample();x['tasks']=231
        self.assertIsNone(p.check(x,now=1))
        self.assertEqual(p.check(x,now=1.25),'ABORTED_TASKS')

    def test_pressure_requires_ten_seconds_and_recovers(self):
        for key,value in (('full',5.01),('some',20.01)):
            p=self.policy();x=sample();x[key]=value
            self.assertIsNone(p.check(x,now=0));self.assertIsNone(p.check(x,now=9.99))
            self.assertEqual(p.check(x,now=10),'ABORTED_PSI')
            p=self.policy();p.check(x,now=1);p.check(sample(),now=2)
            self.assertIsNone(p.check(x,now=3));self.assertIsNone(p.check(x,now=12.99))
            self.assertEqual(p.check(x,now=13),'ABORTED_PSI')

    def test_relaxed_thresholds_allow_transient_pressure_but_not_hard_events(self):
        p=self.policy();x=sample();x.update(full=5,some=20,memory=GIB*.94)
        self.assertIsNone(p.check(x,now=0));self.assertIsNone(p.check(x,now=20))
        x['memory']=GIB*.99
        self.assertIsNone(p.check(x,now=21))
        x['oom_events']=1
        self.assertEqual(p.check(x,now=21.25),'INVALID_HARD_LIMIT')

    def test_unreadable_incomplete_negative_nonfinite_metrics_fail_closed(self):
        for x in (None,{}, {'available':100},sample()):
            if isinstance(x,dict) and 'tasks'in x:x['tasks']=-1
            self.assertEqual(self.policy().check(x,now=1),'ABORTED_METRICS')
        for value in (float('nan'),float('inf'),'0',True):
            x=sample();x['full']=value
            self.assertEqual(self.policy().check(x,now=1),'ABORTED_METRICS')

    def test_identity_controller_heartbeat_and_deadline(self):
        for options,reason in (({'identity_valid':False},'ABORTED_IDENTITY'),
            ({'controller_alive':False},'ABORTED_CONTROLLER_LOST'),
            ({'heartbeat_age':2.01},'ABORTED_MONITOR_FAILURE')):
            self.assertEqual(self.policy().check(sample(),now=1,**options),reason)
        self.assertEqual(self.policy().check(sample(),now=1000),'ABORTED_TIMEOUT')

    def test_hard_events_are_invalid_even_if_current_usage_is_low(self):
        for key in ('oom_events','max_events','pids_events'):
            x=sample();x[key]=1
            self.assertEqual(self.policy().check(x,now=1),'INVALID_HARD_LIMIT')

    def test_local_controller_pressure_is_not_hidden_by_unused_aggregate_memory(self):
        aggregate=sample();aggregate['memory']=385*1024**2
        self.assertIsNone(self.policy().check(aggregate,now=1))
        controller=GuardPolicy(memory_limit=192*1024**2,task_limit=64,deadline=1000)
        # A past memory.high event is not itself a stop. 145 MiB is under
        # 95% of this 192 MiB controller, with or without an anon reading.
        local=sample();local.update(memory=145*1024**2,high_events=1)
        self.assertIsNone(controller.check(local,now=1))
        self.assertIsNone(controller.check(local,now=4))
        local['high_events']=-1
        self.assertEqual(controller.check(local,now=1),'ABORTED_METRICS')
        local['high_events']=0;local['memory']=183*1024**2
        limited=GuardPolicy(memory_limit=192*1024**2,task_limit=64,deadline=1000)
        self.assertIsNone(limited.check(local,now=2))
        self.assertEqual(limited.check(local,now=4),'ABORTED_MEMORY')
        fresh=GuardPolicy(memory_limit=384*1024**2,task_limit=128,deadline=1000)
        cached=sample();cached.update(memory=300*1024**2,high_events=116,anon=40*1024**2,file=258*1024**2)
        self.assertIsNone(fresh.check(cached,now=10))
        self.assertIsNone(fresh.check(cached,now=13))
        resident=sample();resident.update(memory=300*1024**2,high_events=4,anon=300*1024**2,file=8*1024**2)
        self.assertIsNone(fresh.check(resident,now=20))
        self.assertIsNone(fresh.check(resident,now=24))
        hot=sample();hot.update(memory=365*1024**2,high_events=4,anon=359*1024**2,file=6*1024**2)
        self.assertIsNone(fresh.check(hot,now=30))
        self.assertEqual(fresh.check(hot,now=32),'ABORTED_MEMORY')

    def test_project_mounts_pass_and_desktop_sockets_do_not(self):
        shared='/srv/fixture/shared'
        self.assertEqual(approved_mounts({'shared':shared}), [(shared, shared)])
        first='/srv/project/shared'
        second='/srv/project/files'
        listed={'mounts':[{'source':first,'destination':first},
                          {'source':second,'destination':second}]}
        self.assertEqual(approved_mounts(listed), [(first, first), (second, second)])
        for path in ('/run/user/1000/wayland-0', '/tmp/.X11-unix/X0',
                     '/run/user/1000/pulse/native', '/run/user/1000/xauth_dnPjwq'):
            with self.assertRaises(ValueError):
                approved_mounts({'mounts':[{'source':path,'destination':path}]})
        with self.assertRaises(ValueError):
            approved_mounts({'shared':'/run/user/1000/pipewire-0'})

    def test_queue_closes_at_eighty_percent(self):
        p=self.policy();x=sample();x['tasks']=204
        self.assertTrue(p.may_enqueue(x));x['tasks']=205
        self.assertFalse(p.may_enqueue(x));self.assertIsNone(p.check(x,now=1))
        self.assertFalse(p.may_enqueue(None))

    def test_pid_reuse_boot_and_cgroup_mismatch_never_authorize_stop(self):
        expected={'pid':123,'starttime':'999','boot_id':'boot','cgroup':'/test/owned'}
        self.assertTrue(same_execution(expected,expected))
        for key in expected:
            changed=deepcopy(expected);changed[key]='other'
            self.assertFalse(same_execution(expected,changed))
            missing=deepcopy(expected);missing.pop(key)
            self.assertFalse(same_execution(missing,missing))
        self.assertFalse(same_execution({},{}))

    def test_invalid_or_lowered_budget_refuses(self):
        for options in ({'reserve':GIB-1},{'memory_limit':0},{'task_limit':0}):
            with self.assertRaises(ValueError):GuardPolicy(**options)


if __name__=='__main__':unittest.main()
