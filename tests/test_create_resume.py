"""Explicit recovery of an unused creation, without replacement or forged records."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from test_env import FakeRuntime, LOCK, service_in
from unifier.errors import UnifierError
from unifier.runtime import (CreateSpec, ExecResult, PodmanRuntime, SLEEPER,
                             container_name, validate_created_spec)

CID = 'a' * 64
IMAGE_ID = 'b' * 64
ZERO_TIME = '0001-01-01T00:00:00Z'


class ResumeRuntime(FakeRuntime):
    def seed(self, spec):
        super().create(spec)
        self.containers[spec.name] = replace(self.containers[spec.name], container_id=CID)
        self.spec = spec
        self.events.clear()

    def verify_created(self, spec, cid):
        self.events.append(('verify', cid))
        info = self.inspect(cid)
        if spec != self.spec or info is None or info.running or info.status != 'created':
            raise UnifierError('unused creation differs')
        return info

    def start(self, name, *, project, env_id):
        info = self._owned(name, project, env_id)
        self.events.append(('start', name))
        self.containers[info.name] = replace(info, running=True, status='running', pid=50,
                                             started_at='2026-11-15T12:00:00Z')

    def stop(self, name, *, project, env_id):
        info = self._owned(name, project, env_id)
        self.events.append(('stop', name))
        self.containers[info.name] = replace(info, running=False, status='exited', pid=0)


class ResumeCreation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = ResumeRuntime()
        self.service, _ = service_in(self.root, self.runtime)
        self.spec = CreateSpec(container_name(self.root, 'debian13'), 'debian13', str(self.root),
                               LOCK['targets']['debian13']['locked_reference'], ())
        self.runtime.seed(self.spec)

    def resume(self):
        return self.service.resume_create('debian13', None, [], expected_container_id=CID)

    def test_resume_reads_real_identity_inventory_and_registers_same_stopped_cid(self):
        record = self.resume()
        self.assertEqual(record['runtime']['container_id'], CID)
        self.assertEqual(record['runtime']['container_name'], self.spec.name)
        self.assertEqual(record['generation'], 1)
        self.assertEqual(record['package_inventory_sha256'],
                         self.service.store.load('debian13')['package_inventory_sha256'])
        self.assertEqual(self.service.store.inventory_path('debian13').read_text(), 'a\t1\nb\t2\n')
        self.assertFalse(self.runtime.inspect(CID).running)
        self.assertFalse(any(e[0] in {'create', 'remove'} for e in self.runtime.events))
        self.assertTrue(all(e[1] == CID for e in self.runtime.events if e[0] in {'start', 'exec', 'stop'}))

    def test_failed_inventory_retains_same_stopped_container_and_no_registration(self):
        self.runtime.fail_inventory = True
        with self.assertRaisesRegex(UnifierError, 'inventory failed'):
            self.resume()
        self.assertFalse(self.runtime.inspect(CID).running)
        self.assertFalse(self.service.store.exists('debian13'))
        self.assertFalse(any(e[0] in {'create', 'remove'} for e in self.runtime.events))
        self.assertEqual([e for e in self.runtime.events if e[0] == 'stop'], [('stop', CID)])

    def test_changed_ownership_does_not_expand_cleanup(self):
        original = self.runtime._captured
        def changed(info, argv):
            if argv and argv[0] == 'dpkg-query':
                self.runtime.containers[info.name] = replace(info, project='/foreign')
                return ExecResult(1, '', 'inventory failed')
            return original(info, argv)
        self.runtime._captured = changed
        with self.assertRaises(UnifierError):
            self.resume()
        self.assertFalse(any(e[0] in {'remove', 'stop'} for e in self.runtime.events))
        self.assertFalse(self.service.store.exists('debian13'))

    def test_regular_create_still_refuses_existing_container(self):
        with self.assertRaisesRegex(UnifierError, 'already exists'):
            self.service.create('debian13', None, [])
        self.assertEqual(self.runtime.events, [])

    def test_used_between_admission_and_start_is_rejected_without_start_or_stop(self):
        original = self.runtime.verify_created
        calls = 0
        def drift(spec, cid):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.runtime.containers[spec.name] = replace(self.runtime.inspect(cid),
                    status='exited', started_at='2026-11-15T00:00:00Z')
            return original(spec, cid)
        self.runtime.verify_created = drift
        with self.assertRaisesRegex(UnifierError, 'unused creation'):
            self.resume()
        self.assertFalse(any(e[0] in {'start', 'stop', 'create', 'remove'} for e in self.runtime.events))
        self.assertFalse(self.service.store.exists('debian13'))

    def test_partial_registration_and_used_container_are_not_resumed(self):
        self.service.store.directory.mkdir(parents=True)
        self.service.store.inventory_path('debian13').write_text('old evidence\n')
        with self.assertRaisesRegex(UnifierError, 'registration files'):
            self.resume()
        self.assertEqual(self.runtime.events, [])
        self.service.store.inventory_path('debian13').unlink()
        self.runtime.containers[self.spec.name] = replace(self.runtime.inspect(CID), status='exited')
        with self.assertRaisesRegex(UnifierError, 'unused creation'):
            self.resume()
        self.assertFalse(any(e[0] == 'start' for e in self.runtime.events))


def native_metadata():
    """Shape/alias regression from the 2026-11-15 unused rootless Debian fixture."""
    image_ref = 'docker.io/library/debian@sha256:' + 'c' * 64
    spec = CreateSpec('ml-fixture-debian13', 'debian13', '/fixture/environment', image_ref,
                      (('/fixture/shared', '/fixture/shared'),), network='pasta',
                      devices=('/dev/dri/renderD128',))
    env = ['PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin']
    cfg = {'Labels': {'multilinux.project': spec.project, 'multilinux.env': spec.env_id,
                      'multilinux.role': 'environment'}, 'Image': image_ref, 'User': '0:0',
           'WorkingDir': '/', 'Entrypoint': [SLEEPER], 'Cmd': ['infinity'],
           'StopTimeout': 8, 'Hostname': CID[:12], 'Env': env + ['container=podman']}
    data = {'Id': CID, 'Name': spec.name, 'Image': IMAGE_ID,
            'ImageDigest': 'sha256:' + 'd' * 64, 'ImageName': image_ref,
            'StaticDir': '/storage/overlay-containers/'+CID+'/userdata',
            'State': {'Status': 'created', 'Running': False, 'Pid': 0, 'ExitCode': 0,
                      'StartedAt': ZERO_TIME, 'FinishedAt': ZERO_TIME}, 'RestartCount': 0,
            'Path': '/run/podman-init', 'Args': ['--', SLEEPER, 'infinity'], 'Config': cfg,
            'HostConfig': {'Init': True, 'NetworkMode': 'pasta', 'IpcMode': 'private',
                          'PidMode': 'private', 'UTSMode': 'private', 'UsernsMode': '',
                          'RestartPolicy': {'Name': 'no'}, 'Devices': []},
            'Mounts': [{'Type': 'bind', 'Source': '/fixture/shared',
                        'Destination': '/fixture/shared', 'RW': True, 'Mode': ''}]}
    image = {'Id': IMAGE_ID, 'RepoDigests': [image_ref], 'Os': 'linux',
             'Architecture': 'amd64', 'Config': {'Env': env}}
    oci = {'linux': {'namespaces': [{'type': kind} for kind in
                    ('pid', 'network', 'ipc', 'uts', 'mount', 'cgroup')]},
           'process': {'args': ['/run/podman-init', '--', SLEEPER, 'infinity'],
                       'user': {'uid': 0, 'gid': 0}, 'env': env + ['container=podman']},
           'mounts': [{'type': 'bind', 'source': '/fixture/shared', 'destination': '/fixture/shared',
                       'options': ['rprivate', 'rbind']},
                      {'type': 'bind', 'source': '/usr/libexec/podman/catatonit',
                       'destination': '/run/podman-init', 'options': ['bind', 'ro', 'private']},
                      {'type': 'bind', 'source': spec.devices[0], 'destination': spec.devices[0],
                       'options': ['slave', 'nosuid', 'noexec', 'rw', 'rbind']}]}
    saved = {'id': CID, 'name': spec.name, 'rootfsImageID': IMAGE_ID,
             'rootfsImageName': image_ref, 'staticDir': data['StaticDir'], 'spec': oci}
    return spec, data, image, saved


class NativeResumeVerification(unittest.TestCase):
    def test_failed_start_may_add_only_exact_podman_home_and_hostname_defaults(self):
        spec, data, image, saved = native_metadata()
        data['State']['Error'] = 'conmon failed: exit status 1'
        data['Config']['Env'] += ['HOME=/root', 'HOSTNAME=' + CID[:12]]
        validate_created_spec(spec, CID, data, image, saved)
        for extra in ('HOME=/foreign', 'HOSTNAME=foreign', 'http_proxy=http://foreign',
                      'LD_PRELOAD=/foreign.so', 'EXTRA=1'):
            invalid = deepcopy(data)
            invalid['Config']['Env'].append(extra)
            with self.subTest(extra=extra), self.assertRaisesRegex(UnifierError, 'environment differs'):
                validate_created_spec(spec, CID, invalid, image, saved)
        data['Config']['Hostname'] = 'foreign'
        with self.assertRaisesRegex(UnifierError, 'environment differs'):
            validate_created_spec(spec, CID, data, image, saved)

    def test_pre_start_conmon_diagnostic_preserved_without_relaxing_unused_state(self):
        spec, data, image, saved = native_metadata()
        diagnostic = 'conmon failed: exit status 1'
        data['State']['Error'] = diagnostic
        validate_created_spec(spec, CID, data, image, saved)
        self.assertEqual(data['State']['Error'], diagnostic)
        for changed in ({'Status': 'exited'}, {'Running': True}, {'Pid': 50},
                        {'StartedAt': '2026-11-15T00:00:00Z'},
                        {'FinishedAt': '2026-11-15T00:00:01Z'},
                        {'Error': 'x' * 4097}, {'Error': ['not a diagnostic string']}):
            invalid = deepcopy(data)
            invalid['State'].update(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(UnifierError, 'never-started'):
                validate_created_spec(spec, CID, invalid, image, saved)

    def test_same_image_id_accepts_different_manifest_alias_without_creation(self):
        spec, data, image, saved = native_metadata()
        actions = []
        def runner(argv, **kwargs):
            actions.append(argv)
            value = image if argv[1] == 'image' else [data]
            return ExecResult(0, json.dumps(value), '')
        runtime = PodmanRuntime(runner=runner, check_rootless=False)
        with patch.object(runtime, '_created_configuration', return_value=saved):
            result = runtime.verify_created(spec, CID)
        self.assertEqual(result.container_id, CID)
        self.assertEqual([a[1] for a in actions], ['inspect', 'image'])
        self.assertEqual(actions[0][-1], CID)

    def test_mismatched_image_started_execution_and_extra_device_are_refused(self):
        spec, original, image, original_saved = native_metadata()
        for change in ('image', 'cid', 'started', 'command', 'device', 'namespace', 'hooks'):
            data, saved = deepcopy(original), deepcopy(original_saved)
            if change == 'image': data['Image'] = 'e' * 64
            elif change == 'cid': data['Id'] = 'e' * 64
            elif change == 'started': data['State']['StartedAt'] = '2026-11-15T00:00:00Z'
            elif change == 'command': data['Config']['Entrypoint'] = ['/bin/sh']
            elif change == 'device': saved['spec']['mounts'][-1]['source'] = '/dev/dri/card0'
            elif change == 'namespace': saved['spec']['linux']['namespaces'][0]['path'] = '/proc/1/ns/pid'
            else: saved['spec']['hooks'] = {'prestart': [{'path': '/foreign-hook'}]}
            with self.subTest(change=change), self.assertRaises(UnifierError):
                validate_created_spec(spec, CID, data, image, saved)


if __name__ == '__main__':
    unittest.main()
