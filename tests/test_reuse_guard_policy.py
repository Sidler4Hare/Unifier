"""No real pressure is generated: inject measurements into stop decisions."""
from copy import deepcopy
import unittest
from tools.reuse_guard_policy import GIB, GuardPolicy, same_execution


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

    def test_memory_and_tasks_stop_below_hard_limit(self):
        for key,value,reason in (('memory',GIB*.9,'ABORTED_MEMORY'),
                                 ('tasks',231,'ABORTED_TASKS')):
            p=self.policy();x=sample();x[key]=value
            self.assertIsNone(p.check(x,now=1));self.assertEqual(p.check(x,now=1.25),reason)

    def test_pressure_requires_two_seconds_and_recovers(self):
        for key,value in (('full',3.01),('some',10.01)):
            p=self.policy();x=sample();x[key]=value
            self.assertIsNone(p.check(x,now=1));self.assertIsNone(p.check(x,now=2.99))
            self.assertEqual(p.check(x,now=3),'ABORTED_PSI')
            p=self.policy();p.check(x,now=1);p.check(sample(),now=2)
            self.assertIsNone(p.check(x,now=3));self.assertIsNone(p.check(x,now=4.99))

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
        local=sample();local.update(memory=145*1024**2,high_events=1)
        self.assertEqual(controller.check(local,now=1),'INVALID_RECLAIM_PRESSURE')
        local['high_events']=-1
        self.assertEqual(controller.check(local,now=1),'ABORTED_METRICS')
        local['high_events']=0;local['memory']=173*1024**2
        self.assertIsNone(controller.check(local,now=2))
        self.assertEqual(controller.check(local,now=2.25),'ABORTED_MEMORY')
        fresh=GuardPolicy(memory_limit=384*1024**2,task_limit=128,deadline=1000)
        cached=sample();cached.update(memory=300*1024**2,high_events=116,anon=40*1024**2,file=258*1024**2)
        self.assertIsNone(fresh.check(cached,now=10))
        self.assertIsNone(fresh.check(cached,now=13))
        resident=sample();resident.update(memory=300*1024**2,high_events=4,anon=300*1024**2,file=8*1024**2)
        self.assertIsNone(fresh.check(resident,now=20))
        self.assertIsNone(fresh.check(resident,now=21.9))
        self.assertEqual(fresh.check(resident,now=22),'INVALID_RECLAIM_PRESSURE')

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
