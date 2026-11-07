"""Fail-closed decisions shared by the fixed-CID test supervisor and its tests."""
from dataclasses import dataclass
import math


GIB = 1024**3


@dataclass
class GuardPolicy:
    reserve: int = GIB
    memory_limit: int = GIB
    task_limit: int = 256
    deadline: float = 0
    low_samples: int = 0
    memory_samples: int = 0
    task_samples: int = 0
    pressure_since: float | None = None

    def __post_init__(self):
        if (type(self.reserve) is not int or self.reserve < GIB
                or type(self.memory_limit) is not int or self.memory_limit <= 0
                or type(self.task_limit) is not int or self.task_limit <= 0):
            raise ValueError('invalid safety budget')

    def check(self, sample, *, now, controller_alive=True, identity_valid=True,
              heartbeat_age=0):
        if not identity_valid:
            return 'ABORTED_IDENTITY'
        if not controller_alive:
            return 'ABORTED_CONTROLLER_LOST'
        if not math.isfinite(heartbeat_age) or heartbeat_age > 2:
            return 'ABORTED_MONITOR_FAILURE'
        if not math.isfinite(now) or now >= self.deadline:
            return 'ABORTED_TIMEOUT'
        required = ('available', 'full', 'some', 'memory', 'tasks',
                    'oom_events', 'max_events', 'pids_events')
        if (not isinstance(sample, dict) or any(
                type(sample.get(key)) not in (int, float)
                or not math.isfinite(sample[key]) or sample[key] < 0 for key in required)):
            return 'ABORTED_METRICS'
        if any(sample[key] for key in ('oom_events', 'max_events', 'pids_events')):
            return 'INVALID_HARD_LIMIT'
        self.low_samples = self.low_samples + 1 if sample['available'] < self.reserve else 0
        self.memory_samples = self.memory_samples + 1 if sample['memory'] >= self.memory_limit * .9 else 0
        self.task_samples = self.task_samples + 1 if sample['tasks'] >= self.task_limit * .9 else 0
        high_pressure = sample['full'] > 3 or sample['some'] > 10
        self.pressure_since = ((now if self.pressure_since is None else self.pressure_since)
                               if high_pressure else None)
        if self.low_samples >= 2:
            return 'ABORTED_HOST_RESERVE'
        if self.memory_samples >= 2:
            return 'ABORTED_MEMORY'
        if self.task_samples >= 2:
            return 'ABORTED_TASKS'
        if self.pressure_since is not None and now - self.pressure_since >= 2:
            return 'ABORTED_PSI'
        return None

    def may_enqueue(self, sample):
        """Close the queue before its emergency stop line, never hide a bad sample."""
        return (isinstance(sample, dict) and type(sample.get('tasks')) is int
                and 0 <= sample['tasks'] < self.task_limit * .8)


def same_execution(expected, observed):
    """PID, boot, starttime and cgroup all bind a stop permission."""
    keys = ('pid', 'starttime', 'boot_id', 'cgroup')
    return (isinstance(expected, dict) and isinstance(observed, dict)
            and type(expected.get('pid')) is int and expected['pid'] > 0
            and isinstance(expected.get('starttime'), str) and expected['starttime'].isdigit()
            and isinstance(expected.get('boot_id'), str) and bool(expected['boot_id'])
            and isinstance(expected.get('cgroup'), str) and expected['cgroup'].startswith('/')
            and all(expected.get(key) is not None and expected[key] == observed.get(key)
                    for key in keys))
