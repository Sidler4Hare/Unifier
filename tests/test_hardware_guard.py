"""Offline tests for the v008 DRM fixture exception."""
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch
import tempfile

from tools import hardware_test_guard as guard
from tools import arch_gpu_packages, intel_media_guard, reuse_test_guard as reuse
from tools.vulkan_probe import check_readback
from tools.hardware_guard_policy import (
    FIXTURES, NODE_MAPPING, V100_INPUTS, amd_dnf_effective_limits, amd_event_time_ns, validate_amd_bootstrap_history, validate_container,
    validate_image, validate_oci,
)
from unifier.runtime import CreateSpec

INTEL = FIXTURES['intel']
CID = 'a' * 64
OTHER = 'b' * 64
DEBIAN_CANONICAL_DIGEST = 'sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f'


def vulkan_probe_fields():
    rgba = bytearray(bytes((0, 255, 0, 255)) * 512)
    for y in range(4, 12):
        offset = 4*(y*32+8)
        rgba[offset:offset+64] = bytes((255, 0, 0, 255)) * 16
    results = struct.pack('<16I16f', *[(13*i-179) % 2**32 for i in range(16)],
                          *[-3.125 + i*0.375 for i in range(16)])
    output = {'vendor': '8086', 'device': '9a49', 'device_type': 'integrated',
              'pci': '0000:00:02.0', 'render': '226:128', 'driver': 'ANV',
              'device_destroyed': 'true', 'icd': '/usr/lib/libvulkan_intel.so',
              'api': '1.4.305', 'driver_info': 'Mesa', 'device_name': 'Intel Iris Xe',
              'allocated_bytes': '4096', 'rgba': rgba.hex(), 'compute': results.hex()}
    return output


def create_spec(**overrides):
    return {'fixture': 'intel', 'phase': 'create', 'seconds': 120, 'unit': 'unifier-drm-offline',
            **overrides}


def inspect_fixture():
    return {
        'Id': CID, 'Name': INTEL.name, 'Image': INTEL.image_id,
        'ImageName': INTEL.reference, 'ImageDigest': DEBIAN_CANONICAL_DIGEST,
        'State': {'Running': True},
        'Config': {'Labels': INTEL.labels, 'User': '0:0', 'Env': []},
        'HostConfig': {'Devices': [], 'NetworkMode': 'pasta',
                       'CgroupParent': INTEL.slice, 'Memory': 384*1024**2,
                       'MemorySwap': 512*1024**2, 'PidsLimit': 128, 'NanoCpus': 1000000000,
                       'IpcMode': 'private', 'PidMode': 'private', 'RestartPolicy': {'Name': 'no'}},
        'Mounts': [{'Type': 'bind', 'Source': str(INTEL.shared),
                    'Destination': str(INTEL.shared), 'RW': True}],
    }


class FixturePolicyTests(TestCase):
    def test_egl_uses_only_the_two_existing_intel_fixtures_and_pinned_source(self):
        spec = {'fixture': 'intel', 'phase': 'egl', 'seconds': 100,
                'unit': 'unifier-drm-egl-offline'}
        arch = {**spec, 'fixture': 'arch-intel', 'unit': 'unifier-drm-arch-egl-offline'}
        self.assertIs(guard.validate_spec(spec), INTEL)
        self.assertIs(guard.validate_spec(arch), FIXTURES['arch-intel'])
        for change in ({'fixture': 'amd'}, {'fixture': 'arch-intel'}, {'seconds': 101},
                       {'command': ['/usr/bin/true']}, {'gpu_payload_bytes': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.validate_spec({**spec, **change})
        attempt = SimpleNamespace(fixture=INTEL)
        argv = guard.HardwareAttempt.egl_argv(attempt)
        self.assertEqual(argv[:4], [guard.PYTHON, '-I', '-B', '-c'])
        self.assertEqual(argv[5:], ['render', '--pci', INTEL.pci])
        self.assertEqual(hashlib.sha256(argv[4].encode()).hexdigest(), guard.EGL_PROBE_SHA256)

    def test_larger_controller_is_limited_to_the_standard_qq_launch(self):
        spec = create_spec(phase='gpu', unit='unifier-drm-qq-gui-standard-visible',
                           seconds=180, qq_gui=True, controller=guard.QQ_STANDARD_CONTROLLER,
                           gpu_payload_bytes=64*reuse.MIB, command=[guard.PYTHON, '/tmp/qq.py'])
        self.assertIs(guard.validate_spec(spec), INTEL)
        self.assertEqual(guard.selected_controller_budget(spec), guard.QQ_STANDARD_CONTROLLER)
        self.assertEqual(guard.selected_controller_budget({}), reuse.DEFAULT_CONTROLLER)
        with self.assertRaises(RuntimeError):
            reuse.controller_budget(spec)
        for edit in ({'qq_gui': False}, {'unit': 'unifier-drm-qq-gui-other'},
                     {'controller': {**guard.QQ_STANDARD_CONTROLLER, 'memory_high': 512*reuse.MIB}}):
            with self.subTest(edit=edit), self.assertRaises(RuntimeError):
                guard.validate_spec({**spec, **edit})

    def test_arch_recovery_only_admits_fixed_controller_and_fixture(self):
        spec = {'fixture': 'arch-intel', 'phase': 'replay', 'seconds': 120,
                'unit': 'unifier-drm-arch-replay-offline', 'controller_sha256': 'a'*64,
                'command': [guard.PYTHON, str(guard.ARCH_EVIDENCE/'arch-replay.py')]}
        self.assertIs(guard.validate_spec(spec), FIXTURES['arch-intel'])
        corrected = {**spec, 'phase': 'replay-llvm', 'unit': 'unifier-drm-arch-replay-llvm-offline',
                     'command': [guard.PYTHON, str(guard.ARCH_EVIDENCE/'arch-replay-llvm.py')]}
        self.assertIs(guard.validate_spec(corrected), FIXTURES['arch-intel'])
        for edit in ({'fixture': 'intel'}, {'controller_sha256': 'wrong'}, {'seconds': 121},
                     {'command': [guard.PYTHON, '/tmp/replay.py']}, {'phase': 'reconcile'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                guard.validate_spec({**spec, **edit})

    def test_arch_offline_plan_rejects_wrong_snapshot_paths_and_sizes(self):
        policy = arch_gpu_packages
        plan = {'cid': CID, 'archive': policy.ARCHIVE, 'mode': 'egl',
                'requests': policy.VARIANTS['egl'], 'transfer_bytes': 50, 'installed_bytes': 40,
                'repositories': {
                    repo: {'url': f'{policy.ARCHIVE}/{repo}/os/x86_64/{repo}.db',
                           'download_bytes': 10, 'sha256': 'b'*64}
                    for repo in ('core', 'extra')},
                'packages': {'mesa': {'name': 'mesa', 'repo': 'extra',
                                      'filename': 'mesa-1-1-x86_64.pkg.tar.zst', 'sha256': 'c'*64,
                                      'pgp_signature': 'signed', 'download_bytes': 30, 'installed_bytes': 40}}}
        self.assertIs(policy.validate_plan(plan, CID), plan)
        for edit in ({'cid': OTHER}, {'transfer_bytes': 51}, {'archive': 'https://elsewhere.invalid'},
                     {'requests': ['mesa']}, {'packages': {'mesa': {
                         **plan['packages']['mesa'], 'filename': 'mesa-../library.pkg.tar.zst'}}}):
            with self.subTest(edit=edit), self.assertRaises(RuntimeError):
                policy.validate_plan({**plan, **edit}, CID)

    def test_arch_intel_phases_use_only_the_approved_headless_fixture(self):
        fixture = FIXTURES['arch-intel']
        create = {'fixture': 'arch-intel', 'phase': 'create', 'seconds': 120,
                  'unit': 'unifier-drm-arch-create-local'}
        self.assertIs(guard.validate_spec(create), fixture)
        self.assertEqual(fixture.env_id, 'arch')
        self.assertEqual(fixture.devices, INTEL.devices)
        self.assertEqual(fixture.pci, INTEL.pci)
        self.assertEqual(fixture.slice, INTEL.slice)
        self.assertEqual(fixture.name, 'ml-dce69f86-arch')
        self.assertEqual(guard.fixture_create_spec(fixture).mounts,
                         ((str(fixture.shared), str(fixture.shared)),))
        vulkan = {**create, 'phase': 'vulkan', 'unit': 'unifier-drm-arch-vulkan-local',
                  'probe_sha256': 'a'*64}
        self.assertIs(guard.validate_spec(vulkan), fixture)
        for edit in ({'seconds': 121}, {'fixture': 'intel'}, {'probe_sha256': 'x'},
                     {'command': ['/usr/bin/python3']}, {'phase': 'resume'},
                     {'unit': 'unifier-drm-vulkan-local'}, {'project': '/tmp/arbitrary'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                guard.validate_spec({**vulkan, **edit})
        with self.assertRaises(ValueError):
            guard.validate_spec({**create, 'command': ['/usr/bin/python3']})
        prepare = {**create, 'phase': 'prepare', 'unit': 'unifier-drm-arch-prepare-local',
                   'command': ['/usr/bin/python3', '/tmp/controller.py'], 'arch_plan_sha256': 'a'*64}
        self.assertIs(guard.validate_spec(prepare), fixture)
        for edit in ({'arch_plan_sha256': None}, {'fixture': 'intel'}, {'phase': 'gpu'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                guard.validate_spec({**prepare, **edit})

    def test_server_arch_create_requires_the_two_pinned_device_profiles(self):
        amd = {'fixture': 'arch-amd', 'phase': 'create', 'unit': 'unifier-drm-arch-amd-create-reviewed',
               'seconds': 120, 'supervisor_memory_mb': 96, 'supervisor_tasks': 20,
               'acl_guard_unit': 'unifier-v008-amd-acl-arch-create.service'}
        cuda = {'fixture': 'arch-v100', 'phase': 'create', 'unit': 'unifier-arch-v100-create-reviewed',
                'seconds': 120}
        drm, gpu = guard.validate_spec(amd), guard.validate_spec(cuda)
        self.assertEqual((drm.env_id, gpu.env_id), ('arch', 'arch'))
        self.assertEqual(drm.devices, FIXTURES['amd'].devices)
        self.assertEqual(gpu.devices, ())
        self.assertEqual(gpu.cuda, FIXTURES['v100-a'].cuda)
        self.assertEqual(drm.reference, gpu.reference)
        self.assertNotEqual(drm.root, gpu.root)
        for edit in ({'supervisor_memory_mb': 48}, {'seconds': 121}, {'acl_guard_unit': ''},
                     {'phase': 'create', 'project': '/tmp/replacement'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                guard.validate_spec({**amd, **edit})
        with self.assertRaises(ValueError):
            guard.validate_spec({**cuda, 'phase': 'gpu', 'unit': 'unifier-arch-v100-gpu-reviewed',
                                 'tensor_sha256': {name: 'a'*64 for name in guard.V100_TENSOR_FILES}})

    def test_media_solver_must_keep_the_exact_signed_install_only_closure(self):
        row = 'Inst ffmpeg (7:7.1.5-0+deb13u1 Debian:13/stable [amd64])'
        summary = '0 upgraded, 1 newly installed, 0 to remove and 5 not upgraded.'
        expected = {'ffmpeg': ('7:7.1.5-0+deb13u1', 'amd64')}
        intel_media_guard.check_simulated_closure(row+'\n'+summary, expected)
        for output in (row.replace('7.1.5', '7.1.6')+'\n'+summary,
                       row.replace('Inst ffmpeg ', 'Inst ffmpeg [7:7.1.4] ')+'\n'+summary,
                       row+'\nRemv example [1]\n'+summary):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                intel_media_guard.check_simulated_closure(output, expected)

    def test_vulkan_phase_stays_on_intel_and_disallows_an_external_command(self):
        spec = {'fixture': 'intel', 'phase': 'vulkan', 'seconds': 120,
                'unit': 'unifier-drm-vulkan-offline', 'probe_sha256': 'a'*64}
        self.assertIs(guard.validate_spec(spec), INTEL)
        for edit in ({'fixture': 'amd'}, {'fixture': 'v100-a'}, {'seconds': 121},
                     {'probe_sha256': 'not-a-digest'}, {'command': ['/bin/true']}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                guard.validate_spec({**spec, **edit})

    def test_vulkan_oracle_rejects_wrong_driver_and_independent_output_errors(self):
        output = vulkan_probe_fields()

        def lines(fields):
            return '\n'.join(f'{key}={value}' for key, value in fields.items())

        self.assertEqual(check_readback(lines(output))['image']['rgba8_pixels'], 512)
        for key, value in (('icd', '/usr/lib/libvulkan_lvp.so'), ('render', '226:129'),
                           ('rgba', 'ff'+output['rgba'][2:]), ('compute', '00'+output['compute'][2:])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_readback(lines({**output, key: value}))

    def test_v100_phase_is_fixed_to_one_approved_card_and_internal_command(self):
        spec = {'fixture': 'v100-a', 'phase': 'create', 'seconds': 120,
                'unit': 'unifier-v100-a-create-validated'}
        fixture = guard.validate_spec(spec)
        self.assertEqual(fixture.cuda.minor, 0)
        for edit in ({'fixture': 'v100-b'}, {'phase': 'gpu'}, {'seconds': 121},
                     {'command': ['/bin/true']}, {'phase': 'prepare'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                guard.validate_spec({**spec, **edit})

        digests = {name: 'b'*64 for name in guard.V100_TENSOR_FILES}
        gpu = {**spec, 'phase': 'gpu', 'unit': 'unifier-v100-a-gpu-tensor',
               'tensor_sha256': digests}
        self.assertEqual(guard.validate_spec(gpu), fixture)
        for malformed in (None, {}, {**digests, 'extra': 'b'*64},
                          {name: 'sha256:'+'b'*64 for name in digests}):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                guard.validate_spec({**gpu, 'tensor_sha256': malformed})
        with self.assertRaises(ValueError):
            guard.validate_spec({**spec, 'tensor_sha256': digests})
        video = {name: 'c'*64 for name in guard.v100_media_probe.FILES}
        video_spec = {**spec, 'phase': 'gpu', 'unit': 'unifier-v100-a-gpu-video', 'video_sha256': video}
        self.assertEqual(guard.validate_spec(video_spec), fixture)
        with self.assertRaises(ValueError):
            guard.validate_spec({**video_spec, 'tensor_sha256': digests})

    def test_v100_saved_oci_rejects_other_card_and_writable_inputs(self):
        fixture = FIXTURES['v100-b']
        nodes = [{**NODE_MAPPING, 'source': node, 'destination': node}
                 for node in fixture.cuda.nodes]
        inputs = [{'type': 'bind', 'source': str(source), 'destination': destination,
                   'options': ['ro', 'nosuid', 'nodev', 'rprivate', 'rbind']}
                  for destination, (source, _) in V100_INPUTS.items()]
        init = {'type': 'bind', 'source': '/usr/bin/catatonit', 'destination': '/run/podman-init',
                'options': ['bind', 'ro', 'private']}
        good = [*nodes, *inputs, init]
        validate_oci(fixture, {'linux': {}, 'mounts': good})
        for mounts in ([*good, {**NODE_MAPPING, 'source': '/dev/nvidia0', 'destination': '/dev/nvidia0'}],
                       [*nodes, {**inputs[0], 'options': ['rw']}, *inputs[1:], init],
                       [*good, inputs[0]]):
            with self.assertRaises(RuntimeError):
                validate_oci(fixture, {'linux': {}, 'mounts': mounts})

    def test_only_explicit_pair_has_a_create_profile(self):
        self.assertIs(guard.validate_spec(create_spec()), INTEL)
        for edit in ({'fixture': 'v100'}, {'seconds': 301}, {'seconds': True},
                     {'unit': 'arbitrary.service'}, {'project': '/tmp/anything'},
                     {'command': ['/usr/bin/python3', 'anything.py']}, {'gpu_payload_bytes': 1},
                     {'fixture': 'amd'}, {'acl_guard_unit': 'unifier-v008-amd-acl-test.service'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                guard.validate_spec(create_spec(**edit))
        self.assertIs(guard.validate_spec(create_spec(fixture='amd',
            acl_guard_unit='unifier-v008-amd-acl-fixture-test.service')), FIXTURES['amd'])

    def test_amd_supervisor_budget_is_an_exact_opt_in(self):
        amd = create_spec(fixture='amd', acl_guard_unit='unifier-v008-amd-acl-fixture-test.service')
        for tasks in (12, 19, 21, '20', 20.0, True, None):
            with self.subTest(tasks=tasks), self.assertRaisesRegex(ValueError, '20-task AMD'):
                guard.validate_spec({**amd, 'supervisor_tasks': tasks})
        with self.assertRaisesRegex(ValueError, '20-task AMD'):
            guard.validate_spec(create_spec(supervisor_tasks=20))
        with self.assertRaisesRegex(ValueError, '96 MiB AMD'):
            guard.validate_spec({**amd, 'supervisor_memory_mb': 96})
        for memory in (48, 95, 97, '96', 96.0, True, None):
            with self.subTest(memory=memory), self.assertRaisesRegex(ValueError, '96 MiB AMD'):
                guard.validate_spec({**amd, 'supervisor_tasks': 20, 'supervisor_memory_mb': memory})

        specfile = Path('/unused/spec.json')
        increased_tasks = {**amd, 'supervisor_tasks': 20}
        profiles = ((amd, 12, 48, 36), (increased_tasks, 20, 48, 36),
                    ({**increased_tasks, 'supervisor_memory_mb': 96}, 20, 96, 80))
        for spec, tasks, memory, high in profiles:
            with self.subTest(tasks=tasks, memory=memory), patch.object(guard, 'bound_read', return_value=spec), \
                 patch.object(guard, 'trusted_fixture_path'), \
                 patch.object(guard.os, 'getuid', return_value=1001), \
                 patch.object(guard.os, 'geteuid', return_value=1001), \
                 patch.object(guard.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='agent')):
                attempt = guard.HardwareAttempt(specfile)
                self.assertEqual(attempt.monitor_budget, {'memory_max': memory*guard.MIB,
                    'memory_high': high*guard.MIB, 'tasks_max': tasks})
                self.assertEqual(attempt.admission_monitor_bytes, (memory+16)*guard.MIB)

    def test_cached_image_requires_digest_id_reference_size_and_architecture(self):
        image = {'Id': INTEL.image_id, 'Digest': INTEL.digest, 'Size': INTEL.image_size,
                 'Architecture': 'amd64', 'Os': 'linux', 'RepoDigests': [INTEL.reference]}
        validate_image(INTEL, image)
        for key in image:
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                validate_image(INTEL, {**image, key: 'mismatch'})

    def test_container_binds_requested_reference_and_config_ids_canonical_digest(self):
        reference_image = {'Id': INTEL.image_id, 'Digest': INTEL.digest, 'Size': INTEL.image_size,
                           'Architecture': 'amd64', 'Os': 'linux', 'RepoDigests': [INTEL.reference]}
        canonical_reference = INTEL.registry+'@'+DEBIAN_CANONICAL_DIGEST
        config_image = {**reference_image, 'Digest': DEBIAN_CANONICAL_DIGEST,
                        'RepoDigests': [INTEL.reference, canonical_reference]}
        with tempfile.TemporaryDirectory() as temporary:
            attempt = guard.HardwareAttempt.__new__(guard.HardwareAttempt)
            attempt.fixture = INTEL
            attempt.out = Path(temporary)
            with patch.object(reuse, 'call', side_effect=[
                    guard.json.dumps([reference_image]), guard.json.dumps([config_image])]) as native:
                attempt.verify_cached_image()
            self.assertEqual([args.args[0][-1] for args in native.call_args_list],
                             [INTEL.reference, INTEL.image_id])
            proof = reuse.get(attempt.out/'image-before.json')
            self.assertEqual(proof['container_digest'], DEBIAN_CANONICAL_DIGEST)
        value = inspect_fixture()
        validate_container(INTEL, value, cid=CID, image_digest=proof['container_digest'])
        for changed in ({'ImageDigest': INTEL.digest}, {'ImageName': canonical_reference},
                        {'Image': OTHER}):
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, 'identity or image'):
                validate_container(INTEL, {**value, **changed}, cid=CID, image_digest=proof['container_digest'])
        for repo_digests in ([INTEL.reference], [canonical_reference],
                             [INTEL.reference, 'localhost/debian@'+DEBIAN_CANONICAL_DIGEST]):
            with self.subTest(repo_digests=repo_digests), self.assertRaises(RuntimeError):
                validate_image(INTEL, {**config_image, 'RepoDigests': repo_digests}, by_config_id=True)

    def test_only_one_device_and_no_cdi_or_host_security_options(self):
        value = inspect_fixture()
        validate_container(INTEL, value, cid=CID, image_digest=DEBIAN_CANONICAL_DIGEST)
        for edit in ({'Devices': [{'PathInContainer': '/dev/nvidia0'}]}, {'DeviceRequests': ['cdi']},
                     {'SecurityOpt': ['label=disable']}, {'NetworkMode': 'host'}):
            invalid = {**value, 'HostConfig': {**value['HostConfig'], **edit}}
            with self.subTest(edit=edit), self.assertRaisesRegex(RuntimeError, 'device'):
                validate_container(INTEL, invalid, cid=CID, image_digest=DEBIAN_CANONICAL_DIGEST)
        for mounts in ([], [{**NODE_MAPPING, 'options': ['ro', 'rbind']}],
                       [NODE_MAPPING, {**NODE_MAPPING, 'source': '/dev/dri/card0'}],
                       [NODE_MAPPING, {'type': 'bind', 'source': '/home/omg', 'destination': '/home/user'}]):
            with self.subTest(mounts=mounts), self.assertRaises(RuntimeError):
                validate_oci(INTEL, {'linux': {}, 'mounts': mounts})
        validate_oci(INTEL, {'linux': {}, 'mounts': [NODE_MAPPING]})

    def test_old_entry_still_rejects_devices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root/'.lab/state/environments/debian13.json'
            record.parent.mkdir(parents=True)
            config = {'cid': CID, 'project': str(root), 'env_id': 'debian13',
                      'environment_uuid': 'record-uuid', 'generation': 1, 'shared': str(INTEL.shared)}
            reuse.put(record, {'environment_uuid': config['environment_uuid'], 'generation': 1,
                               'runtime': {'container_id': CID}})
            value = inspect_fixture()
            value['Config']['Labels']['multilinux.project'] = str(root)
            value['HostConfig'].update(Memory=768*1024**2, MemorySwap=896*1024**2, PidsLimit=192)
            with patch.object(reuse, 'call', side_effect=lambda *_: guard.json.dumps([value])):
                self.assertEqual(reuse.info(config), value)
                value['HostConfig']['Devices'] = [{'PathInContainer': '/dev/dri/renderD128'}]
                with self.assertRaisesRegex(RuntimeError, 'limits/isolation'):
                    reuse.info(config)


class HardwareDiskBudgetTests(TestCase):
    def test_previous_host_disk_use_does_not_count_toward_the_current_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = guard.HardwareAttempt.__new__(guard.HardwareAttempt)
            attempt.base = root
            attempt.out = root / "attempt"
            attempt.out.mkdir()
            attempt.sample_roots = (root, attempt.out)
            device = str(root.stat().st_dev)
            original = {device: 80 * 1024 * guard.MIB}
            reuse.put(root / "disk-baseline.json", original)
            volume = SimpleNamespace(f_bavail=70 * 1024, f_frsize=guard.MIB,
                                     f_blocks=100 * 1024)
            with patch.object(guard.os, "statvfs", return_value=volume):
                attempt.check_disk(initial=True)
                self.assertEqual(reuse.get(attempt.out / "disk-stage-before.json"),
                                 {device: 70 * 1024 * guard.MIB})
                self.assertEqual(reuse.get(root / "disk-baseline.json"), original)
                volume.f_bavail -= 1024
                attempt.check_disk()
                volume.f_bavail -= 1
                with self.assertRaisesRegex(RuntimeError, "allocated disk budget"):
                    attempt.check_disk()
                volume.f_bavail = 9 * 1024
                with self.assertRaisesRegex(RuntimeError, "disk reserve"):
                    attempt.check_disk()


class EventWindowTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        self.attempt = guard.HardwareAttempt.__new__(guard.HardwareAttempt)
        self.attempt.fixture = INTEL
        self.attempt.phase = 'create'
        self.attempt.out = self.out
        self.attempt.config = {'cid': None}
        self.attempt.create_event = None
        self.attempt.start_seen = False
        self.event = {'Name': INTEL.name, 'Attributes': INTEL.labels, 'timeNano': 101}
        reuse.put(self.out/'create-intent.json', {'at_ns': 100})

    def test_exact_early_event_must_converge_on_returned_cid(self):
        self.attempt.event(self.event, CID, 'create')
        self.assertEqual(self.attempt.create_event, CID)
        with self.assertRaisesRegex(RuntimeError, 'unexpected native'):
            self.attempt.event(self.event, CID, 'start')
        reuse.put(self.out/'created-container.json', {'cid': CID})
        reuse.put(self.out/'start-intent.json', {'at_ns': 102})
        self.attempt.event({**self.event, 'timeNano': 103}, CID, 'start')
        with self.assertRaisesRegex(RuntimeError, 'second start'):
            self.attempt.event({**self.event, 'timeNano': 104}, CID, 'start')

    def test_amd_podman_rfc3339_event_binds_same_approved_create_and_start(self):
        self.attempt.fixture = fixture = FIXTURES['amd']
        event = {'Name': fixture.name, 'Attributes': fixture.labels,
                 'Time': '2026-11-15T20:12:08.402039208+08:00'}
        self.assertEqual(amd_event_time_ns(event), 1794744728402039208)
        reuse.put(self.out/'create-intent.json', {'at_ns': 1794744728402039208})
        early = {**event, 'Time': '2026-11-15T20:12:08.402039207+08:00'}
        for invalid in (early, {**event, 'Time': '2026-11-15T20:12:08'}, {**event, 'timeNano': 'invalid'}):
            with self.subTest(event=invalid), self.assertRaisesRegex(RuntimeError, 'unexpected create'):
                self.attempt.event(invalid, CID, 'create')
        self.attempt.event(event, CID, 'create')
        self.assertEqual(self.attempt.create_event, CID)
        reuse.put(self.out/'created-container.json', {'cid': CID})
        reuse.put(self.out/'start-intent.json', {'at_ns': 1794744728499944057})
        self.attempt.event({**event, 'Time': '2026-11-15T20:12:08.769474739+08:00'}, CID, 'start')
        self.assertTrue(self.attempt.start_seen)
        with self.assertRaisesRegex(RuntimeError, 'second start'):
            self.attempt.event({**event, 'Time': '2026-11-15T12:12:09Z'}, CID, 'start')

    def test_intel_resume_accepts_only_start_for_already_created_cid(self):
        self.attempt.phase = 'resume'
        cid = guard.INTEL_RESUME_CID
        self.assertEqual(self.attempt.active_config()['cid'], cid)
        with self.assertRaisesRegex(RuntimeError, 'unexpected create'):
            self.attempt.event(self.event, cid, 'create')
        reuse.put(self.out/'start-intent.json', {'at_ns': 100})
        with self.assertRaisesRegex(RuntimeError, 'unexpected native'):
            self.attempt.event(self.event, OTHER, 'start')
        self.attempt.event(self.event, cid, 'start')
        with self.assertRaisesRegex(RuntimeError, 'second start'):
            self.attempt.event(self.event, cid, 'start')

    def test_wrong_name_labels_timestamp_non_target_action_or_short_id_fail(self):
        for edit, cid, status in (({'Name': 'other'}, CID, 'create'),
                                  ({'Attributes': {}}, CID, 'create'),
                                  ({'timeNano': 99}, CID, 'create'),
                                  ({}, CID[:12], 'create'), ({}, OTHER, 'start'),
                                  ({}, OTHER, 'exec')):
            with self.subTest(edit=edit, cid=cid, status=status), self.assertRaises(RuntimeError):
                self.attempt.event({**self.event, **edit}, cid, status)
        reuse.put(self.out/'created-container.json', {'cid': CID})
        with self.assertRaisesRegex(RuntimeError, 'unexpected create'):
            self.attempt.event(self.event, OTHER, 'create')

    def test_creation_is_single_use_and_runtime_never_removes(self):
        self.attempt.once = self.out/'create-attempt.json'
        reuse.put(self.attempt.once, {'attempt': str(self.out)})
        runtime = guard.HardwareRuntime(self.attempt)
        with patch.object(guard, '_run_podman') as native:
            with self.assertRaisesRegex(RuntimeError, 'one-time'):
                runtime.create(CreateSpec('foreign', INTEL.env_id, str(INTEL.root),
                                           INTEL.reference, ()))
            with self.assertRaisesRegex(RuntimeError, 'retained'):
                runtime.remove(INTEL.name, project=str(INTEL.root), env_id=INTEL.env_id)
            native.assert_not_called()

    def test_exec_pins_real_operand_not_guest_argument(self):
        self.attempt.active_config = lambda: {'cid': CID}
        runtime = guard.HardwareRuntime(self.attempt)
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            runtime.pin_exec(['podman', 'exec', '--env', 'FOO=1', OTHER, '/bin/echo', CID])
        self.assertEqual(runtime.pin_exec(['podman', 'exec', INTEL.name, '/bin/true']),
                         ['podman', 'exec', CID, '/bin/true'])

    def test_timeout_is_bounded_by_remaining_amd_acl_lease(self):
        attempt = SimpleNamespace(fixture=FIXTURES['amd'], out=self.out)
        reuse.put(self.out/'acl-watchdog.json', {'deadline': 145.0})
        with patch.object(guard.time, 'monotonic', return_value=50):
            self.assertEqual(guard.command_timeout(attempt, 120), 20)
        with patch.object(guard.time, 'monotonic', return_value=71):
            with self.assertRaisesRegex(RuntimeError, 'remaining time'):
                guard.command_timeout(attempt, 120)
            self.assertTrue((self.out/'queue-closed').exists())
            self.assertEqual(guard.command_timeout(attempt, 120, cleanup=True), 59)
        with patch.object(guard.time, 'monotonic', return_value=130):
            with self.assertRaisesRegex(RuntimeError, 'cleanup deadline'):
                guard.command_timeout(attempt, 120, cleanup=True)


class CudaObservationTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name)
        fixture = FIXTURES['v100-a']
        self.device = fixture.cuda.identity()
        uuid = '8b2d9622-ea94-482c-9767-e7c1e5ccddec'
        self.boot = '57b3e0ed-555f-476a-b360-ea1ca80462cd'
        config = {'cid': CID, 'environment_uuid': uuid, 'generation': 1}
        runtime = {
            'backend': 'podman', 'container_name': fixture.name, 'container_id': CID,
            'project': str(fixture.root), 'network': fixture.network, 'share_label_policy': 'preserve-host',
            'selinux_label': 'default', 'full_init': False, 'user_namespace': 'rootless-default',
            'devices': [], 'ipc': 'private', 'rootless': True, 'cuda': self.device,
        }
        self.record = {
            'id': fixture.env_id, 'target': fixture.env_id, 'environment_uuid': uuid, 'generation': 1,
            'created_by_uid': fixture.uid, 'desktop': None, 'shared_mounts': [], 'runtime': runtime,
            'base_image': {'locked_reference': fixture.reference, 'manifest_digest': fixture.digest},
        }
        self.attempt = SimpleNamespace(
            out=out, record_path=out/'record.json', fixture=fixture, spec={},
            active_config=lambda: config)
        monitor = {'boot_id': self.boot}
        stopped = {'cid': CID, 'status': 'STOPPED', 'state': {'Running': False, 'Pid': 0},
                   'worker_final': {'MainPID': '0', 'ControlGroup': ''}}
        self.rows = {
            'record.json': self.record,
            'live-binding.json': {'cid': CID, 'init': monitor, 'coverage_verified': True},
            'ready.json': {'cid': None, 'monitor': monitor},
            'start-intent.json': {'cid': CID, 'before': {'Running': False}},
            'receipt.json': {'monitor': monitor, 'reason': None, 'command_result': {'exit_code': 0},
                             'cleanup': stopped},
            'rescue.json': stopped,
            'gpu-after.json': {'uuid': self.device['uuid'], 'pci': self.device['pci'], 'compute_pids': []},
            'result.json': {'cid': CID, 'expected': self.device, 'status': 'PASS',
                            'formal_stop': 'STOPPED', 'exit_code': 0,
                            'probe': {'uuid': self.device['uuid'], 'pci': self.device['pci'],
                                      'devices': 1, 'sm': 70, 'libcuda': '/run/unifier-cuda/libcuda.so.1',
                                      'driver_api': 12090, 'context_destroyed': True}},
        }

    def observation(self):
        with patch.object(guard, 'bound_read', side_effect=lambda path: self.rows[path.name]):
            return guard.cuda_observation(self.attempt)

    def test_normalized_receipt_matches_hardware_report_schema(self):
        from unifier.hardware_report import hardware_report

        observation = self.observation()
        self.assertEqual(observation['boot_id'], self.boot)
        self.assertEqual(observation['backend_version'], '12090')
        self.assertEqual(observation['selector'], self.device['uuid'])
        report = hardware_report(self.record, observed_binding=observation['environment_binding'],
                                 observations=[observation], evidence=[observation])
        self.assertEqual(report['evidence'][0]['capability_state'], 'available')

    def test_refuses_changed_boot_cleanup_or_device(self):
        for name, key, value in (
            ('ready.json', 'monitor', {'boot_id': 'changed'}),
            ('start-intent.json', 'cid', 'f'*64),
            ('rescue.json', 'worker_final', {'MainPID': '123', 'ControlGroup': '/busy'}),
            ('gpu-after.json', 'pci', '0000:00:01.0'),
            ('gpu-after.json', 'compute_pids', ['123']),
            ('result.json', 'formal_stop', 'UNKNOWN'),
        ):
            with self.subTest(receipt=name, field=key):
                original = self.rows[name]
                self.rows[name] = {**original, key: value}
                self.assertIsNone(self.observation())
                self.rows[name] = original
        (self.attempt.out/'abort.json').touch()
        self.assertIsNone(self.observation())


    def test_video_uses_matched_driver_and_both_native_readbacks(self):
        media = guard.v100_media_probe
        unit = 'media-known-answer'
        digest = 'd'*64
        manifest = {key: digest for key in media.FILES}
        self.attempt.spec.update(unit=unit, video_sha256=manifest)
        scratch = f'/var/tmp/{unit}-video'
        self.rows['gpu-after.json']['driver_version'] = '580.178.04'
        self.rows['gpu-admission.json'] = self.rows['gpu-after.json'].copy()
        report = self.rows['result.json']
        report.update(gpu=self.device, probe_removed=True, probe_sha256=manifest, media_runtime={
            'schema_version': 1,
            'ffmpeg': {'path': scratch+'/ffmpeg', 'sha256': digest, 'version': None},
            'cuda_driver': {'host_library': self.device['library'], 'guest_library': media.CUDA_LIBRARY,
                            'origin': 'pinned-host-mount', 'load_evidence': 'ld-debug-init'},
            'backends': {codec: {'guest_library': f'{scratch}/{name}', 'sha256': digest,
                                 'origin': 'verified-guest-copy', 'load_evidence': 'ld-debug-init'}
                         for codec, name in (('NVDEC', 'libnvcuvid.so.1'), ('NVENC', 'libnvidia-encode.so.1'))},
        }, cases={'NVDEC': {'exit': 0, 'exact': True, 'frames': media.FRAMES},
                  'NVENC': {'exit': 0, 'frames': media.FRAMES, 'psnr_db': [48.2]*media.FRAMES},
                  'software-readback': {'exit': 0}})
        with patch.object(guard, 'bound_read', side_effect=lambda path: self.rows[path.name]):
            observation = guard.cuda_media_observation(self.attempt)
            self.assertEqual(observation['backend_version'], '580.178.04')
            self.assertEqual(observation['oracle']['decode_exact_frames'], media.FRAMES)
            scores = report['cases']['NVENC']['psnr_db']
            scores[0] = 29.9
            self.assertIsNone(guard.cuda_media_observation(self.attempt))
            scores[0] = 48.2
            self.rows['gpu-admission.json']['driver_version'] = '580.168.01'
            self.assertIsNone(guard.cuda_media_observation(self.attempt))


class EglObservationTests(TestCase):
    def test_sidecar_matches_native_renderer_and_frame_and_refuses_fallback(self):
        path = '/dev/dri/renderD128'
        physical = {'sysfs_device': '/devices/pci0000:00/0000:00:02.0',
                    'major': 226, 'minor': 128, 'vendor': '0x8086', 'device': '0x9a49'}
        native = {'path': path, 'rdev': 57984, 'pci': INTEL.pci, 'driver': 'i915',
                  'vendor': '8086', 'device': '9a49'}
        proof = {'environment_binding': {'container_id': CID}}
        probe = {
            'mode': 'render', 'status': 'PASS', 'reason': 'verified', 'requested_pci': INTEL.pci,
            'device': {'node': path, 'pci': INTEL.pci, 'vendor': '8086', 'device': '9a49',
                       'major': 226, 'minor': 128, 'kernel_driver': 'i915', 'open_mode': 'rw', 'euid': 0},
            'backend': {'platform': 'gbm', 'gbm_fd_matches': True, 'surfaceless': True,
                        'egl_render_node': path, 'mesa_driver': 'iris', 'egl_vendor': 'Mesa Project',
                        'egl_version': '1.5', 'gl_vendor': 'Intel',
                        'gl_renderer': 'Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2)',
                        'gl_version': 'OpenGL ES 3.2 Mesa 26.2.4-arch1.1'},
            'frame': {'width': 256, 'height': 256, 'rgba8_bytes': 262144, 'clear_count': 2,
                      'sha256': guard.EGL_FRAME_SHA256},
        }
        digest = guard.EGL_PROBE_SHA256
        report = {'probe': probe, 'probe_sha256': digest}
        device = {'physical': physical, 'native': native}
        attempt = SimpleNamespace(out=Path('/offline'), fixture=INTEL)
        with (patch.object(guard, '_completed_probe', return_value=(proof, report)),
              patch.object(guard, '_completed_drm_device', return_value=device),
              patch.object(guard, 'bound_read', return_value={'cid': CID, 'probe_sha256': digest}),
              patch.object(guard, 'bound_bytes', side_effect=lambda _: json.dumps(probe).encode()),
              patch.object(guard.reuse, 'put') as save):
            observation = guard.egl_observation(attempt)
            self.assertEqual(observation['backend_version'], 'OpenGL ES 3.2 Mesa 26.2.4-arch1.1')
            self.assertEqual(observation['oracle'], {'image_sha256': guard.EGL_FRAME_SHA256})
            self.assertEqual(observation['device_identity'], physical)
            save.assert_called_once_with(attempt.out/'egl-device-after.json', device)
            probe['backend']['gl_renderer'] = 'Mesa llvmpipe (software)'
            self.assertIsNone(guard.egl_observation(attempt))


class VulkanObservationTests(TestCase):
    def test_sidecar_requires_native_oracles_and_unchanged_physical_node(self):
        from unifier.hardware_report import hardware_report
        from unifier.identity import binding_for

        path = '/dev/dri/renderD128'
        physical = {'sysfs_device': '/devices/pci0000:00/0000:00:02.0',
                    'major': 226, 'minor': 128, 'vendor': '0x8086', 'device': '0x9a49'}
        native = {'path': path, 'rdev': 57984, 'pci': '0000:00:02.0',
                  'vendor': '8086', 'device': '9a49'}
        device = {'physical': physical, 'native': native}
        record = {'schema_version': 2, 'id': INTEL.env_id,
                  'environment_uuid': '8b2d9622-ea94-482c-9767-e7c1e5ccddec', 'generation': 1,
                  'runtime': {'container_id': CID, 'devices': [path]}}
        proof = {'environment_binding': binding_for(record), 'schema_version': 1,
                 'boot_id': '57b3e0ed-555f-476a-b360-ea1ca80462cd',
                 'source': 'result.json', 'result': 'PASS', 'capability_state': 'available',
                 'cleanup_state': 'verified', 'software_fallback': False}
        stdout = '\n'.join(f'{key}={value}' for key, value in vulkan_probe_fields().items())
        expected = check_readback(stdout)
        digest = 'd'*64
        attempt = SimpleNamespace(out=Path('/offline'), fixture=INTEL, spec={'probe_sha256': digest})
        report = {'probe': expected, 'probe_removed': True, 'probe_sha256': digest,
                  'icd_manifest': guard.vulkan_probe.ICD}
        rows = {'vulkan-device-before.json': device, 'device-before.json': native,
                'guest-device.json': {'mapping': ['0', str(INTEL.uid), '1'], 'rdev': native['rdev']}}
        with (patch.object(guard, '_completed_probe', return_value=(proof, report)),
              patch.object(guard, 'bound_read', side_effect=lambda p: rows[p.name]),
              patch.object(guard, '_drm_probe_device', return_value=device) as post_device,
              patch.object(guard, 'bound_bytes', return_value=stdout.encode()) as raw,
              patch.object(guard.reuse, 'put') as save):
            observation = guard.vulkan_observation(attempt)
            self.assertEqual(observation['device_identity'], physical)
            self.assertEqual(observation['backend_version'], expected['driver_info'])
            self.assertEqual(observation['oracle']['compute_sha256'], expected['compute']['sha256'])
            classified = hardware_report(record, observed_binding=binding_for(record),
                                         observations=[observation], evidence=[observation])
            self.assertEqual(classified['evidence'][0]['capability_state'], 'available')
            save.assert_called_once_with(attempt.out/'vulkan-device-after.json', device)
            post_device.return_value = {**device, 'physical': {**physical, 'vendor': '0xffff'}}
            self.assertIsNone(guard.vulkan_observation(attempt))
            post_device.return_value = device
            raw.return_value = stdout.replace('render=226:128', 'render=226:129').encode()
            with self.assertRaises(ValueError):
                guard.vulkan_observation(attempt)


class HardwareCleanupTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.config = {'cid': CID}
        self.attempt = SimpleNamespace(cleanup_config=lambda: self.config,
                                       cleanup_timeout=lambda cap, **_: cap)
        self.init = {'pid': 100, 'starttime': '1', 'boot_id': 'same', 'cgroup': '/aggregate/guest/payload'}
        self.scope = {'Id': 'libpod-'+CID+'.scope', 'InvocationID': 'guest-1234',
                      'ControlGroup': '/aggregate/guest'}
        self.worker = {'Id': 'unifier-drm-test-work.service', 'InvocationID': 'worker-1234',
                       'ControlGroup': '/aggregate/worker', 'MainPID': '101'}
        self.exited_worker = {'Id': self.worker['Id'], 'MainPID': '0', 'ControlGroup': ''}
        self.running = {'State': {'Running': True, 'Pid': self.init['pid'], 'StartedAt': 'created'}}
        self.stopped = {'State': {'Running': False, 'Pid': 0, 'StartedAt': 'created'}}
        reuse.put(self.out/'live-binding.json', {'init': self.init, 'scope': self.scope,
                                                  'cid': CID, 'started_at': 'created'})
        reuse.put(self.out/'worker-unit.json', self.worker)
        self.podman_stop = ['podman', 'stop', '--time', '8', CID]
        self.stop_guest_scope = ['systemctl', '--user', 'stop', self.scope['Id']]
        self.stop_worker = ['systemctl', '--user', 'stop', self.worker['Id']]

    def test_stop_timeout_still_stops_verified_guest_scope_and_registered_worker(self):
        with patch.object(reuse, 'info', side_effect=[self.running, self.stopped]), \
             patch.object(reuse, 'unit', side_effect=[self.scope, self.worker, self.exited_worker]), \
             patch.object(reuse, 'process', side_effect=[self.init, self.init, self.init, FileNotFoundError()]), \
             patch.object(reuse, 'call', side_effect=[
                 subprocess.TimeoutExpired(self.podman_stop, 12), '', '']) as native:
            result = reuse.cleanup(self.config, self.out, hardware=self.attempt)
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(result['worker_final'], self.exited_worker)
        self.assertEqual(native.call_args_list, [call(self.podman_stop, timeout=12),
                                               call(self.stop_guest_scope, timeout=10),
                                               call(self.stop_worker, timeout=12)])

    def test_replaced_guest_scope_preserves_unknown_and_stops_only_registered_worker(self):
        replaced = {**self.scope, 'InvocationID': 'foreign'}
        with patch.object(reuse, 'info', return_value=self.running), \
             patch.object(reuse, 'unit', side_effect=[replaced, self.worker, self.exited_worker]), \
             patch.object(reuse, 'process', return_value=self.init), \
             patch.object(reuse, 'call', side_effect=[
                 subprocess.TimeoutExpired(self.podman_stop, 12), '']) as native:
            with self.assertRaisesRegex(RuntimeError, 'exact guest scope changed'):
                reuse.cleanup(self.config, self.out, hardware=self.attempt)
        self.assertEqual(reuse.get(self.out/'cleanup.json')['status'], 'CLEANUP_UNKNOWN')
        self.assertEqual(native.call_args_list, [call(self.podman_stop, timeout=12),
                                               call(self.stop_worker, timeout=12)])

    def test_rescue_unit_has_sixty_seconds_without_extending_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            (out/'spec.json').touch()
            attempt = SimpleNamespace(out=out, spec=create_spec(),
                                      monitor_budget={'memory_max': 64*guard.MIB,
                                                      'memory_high': 48*guard.MIB, 'tasks_max': 16})
            with patch.object(guard.subprocess, 'call', return_value=0) as launch:
                self.assertEqual(guard.run(attempt), 0)
        argv, = launch.call_args.args
        self.assertIn('--property=TimeoutStopSec=60s', argv)
        self.assertIn('--property=RuntimeMaxSec=200', argv)
        self.assertIn('--setenv=LC_ALL=C.UTF-8', argv)
        self.assertIn('--setenv=LANG=C.UTF-8', argv)
        self.assertEqual(guard.clean_environment({'LC_ALL': 'C', 'LANG': 'C'})['LC_ALL'], 'C.UTF-8')


class AmdBootstrapTests(TestCase):
    def test_amd_stopped_layout_may_omit_only_the_recorded_true_stop_diagnostic(self):
        state = {'OciVersion': '1.1.0', 'Status': 'exited', 'Running': False, 'Paused': False,
                 'Restarting': False, 'OOMKilled': False, 'Dead': False, 'Pid': 0, 'ExitCode': 143,
                 'Error': '', 'StartedAt': '2026-11-15T20:12:08.76947062+08:00',
                 'FinishedAt': '2026-11-15T20:12:08.899457461+08:00',
                 'Health': {'Status': '', 'FailingStreak': 0, 'Log': None},
                 'CheckpointedAt': '0001-01-01T00:00:00Z', 'RestoredAt': '0001-01-01T00:00:00Z'}
        receipt = {**state, 'StoppedByUser': True}
        value = {'State': state, 'RestartCount': 0, 'Path': '/run/podman-init',
                 'Args': ['--', '/usr/bin/sleep', 'infinity'], 'HostConfig': {'Init': True},
                 'Config': {'Entrypoint': '/usr/bin/sleep', 'Cmd': ['infinity'], 'StopTimeout': 8}}
        guard.validate_amd_stopped_layout(value, receipt)
        guard.validate_amd_stopped_layout({**value, 'State': receipt}, receipt)
        self.assertIs(receipt['StoppedByUser'], True)
        self.assertNotIn('StoppedByUser', state)
        for changed in ({'StoppedByUser': False}, {'StoppedByUser': None}, {'StoppedByUser': 1},
                        {'ExitCode': 0}, {'OOMKilled': True},
                        {'FinishedAt': '2026-11-15T20:12:09+08:00'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, 'bootstrap state'):
                guard.validate_amd_stopped_layout({**value, 'State': {**state, **changed}}, receipt)

    def test_amd_stopped_layout_accepts_only_the_two_native_sleeper_entrypoint_shapes(self):
        state = {'Status': 'exited', 'Pid': 0}
        value = {'State': state, 'RestartCount': 0, 'Path': '/run/podman-init',
                 'Args': ['--', '/usr/bin/sleep', 'infinity'], 'HostConfig': {'Init': True},
                 'Config': {'Cmd': ['infinity'], 'StopTimeout': 8}}
        for entrypoint in ('/usr/bin/sleep', ['/usr/bin/sleep']):
            with self.subTest(entrypoint=entrypoint):
                value['Config']['Entrypoint'] = entrypoint
                guard.validate_amd_stopped_layout(value, state)
        for entrypoint in ('/bin/sh -c /usr/bin/sleep', ['/bin/sh', '-c', '/usr/bin/sleep'],
                           ['/usr/bin/sleep', 'infinity']):
            with self.subTest(entrypoint=entrypoint), self.assertRaisesRegex(RuntimeError, 'sleeper layout'):
                value['Config']['Entrypoint'] = entrypoint
                guard.validate_amd_stopped_layout(value, state)
        value['Config']['Entrypoint'] = '/usr/bin/sleep'
        with self.assertRaisesRegex(RuntimeError, 'sleeper layout'):
            guard.validate_amd_stopped_layout({**value, 'RestartCount': 1}, state)

    def test_amd_bootstrap_spec_only_admits_exact_original_attempt_and_budget(self):
        spec = create_spec(fixture='amd', phase='amd-bootstrap-recover',
                           bootstrap_from=str(guard.AMD_BOOTSTRAP_FROM),
                           supervisor_memory_mb=96, supervisor_tasks=20,
                           acl_guard_unit='unifier-v008-amd-acl-fresh.service')
        self.assertIs(guard.validate_spec(spec), FIXTURES['amd'])
        for change in ({'bootstrap_from': '/tmp/unknown'}, {'seconds': 121},
                       {'supervisor_memory_mb': 48}, {'supervisor_tasks': 12},
                       {'command': ['/usr/bin/python3', '/tmp/custom.py']}, {'phase': 'create'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.validate_spec({**spec, **change})

    def test_amd_bootstrap_history_excludes_later_exec_or_start(self):
        fixture = FIXTURES['amd']
        cid = guard.AMD_BOOTSTRAP_CID
        base = {'ID': cid, 'Type': 'container', 'Name': fixture.name, 'Image': fixture.reference,
                'Attributes': fixture.labels}
        events = [{**base, 'Status': status, 'Time': stamp} for status, stamp in (
            ('create', '2026-11-15T20:12:08.402039208+08:00'),
            ('init', '2026-11-15T20:12:08.76119939+08:00'),
            ('start', '2026-11-15T20:12:08.769474739+08:00'),
            ('died', '2026-11-15T20:12:08.90591634+08:00'))]
        events[-1]['ContainerExitCode'] = 143
        provenance = {'create_intent': 1794744728341926935, 'start_intent': 1794744728499944057,
                      'aborted': 1794744728738321300, 'command_end': 1794744729079345500,
                      'stopped': {'StartedAt': '2026-11-15T20:12:08.76947062+08:00',
                                  'FinishedAt': '2026-11-15T20:12:08.899457461+08:00'}}
        end = {**base, 'Status': 'cleanup', 'Time': '2026-11-15T20:12:09+08:00'}
        validate_amd_bootstrap_history(fixture, cid, events, events+[end], **provenance)
        for status in ('exec', 'start', 'create', 'update'):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'later activity'):
                validate_amd_bootstrap_history(fixture, cid, events, events+[{**end, 'Status': status}],
                                               **provenance)
        with self.assertRaisesRegex(RuntimeError, 'previously bound init'):
            validate_amd_bootstrap_history(fixture, cid, events, events,
                                           **{**provenance, 'aborted': provenance['command_end']})

    def test_amd_bootstrap_calls_only_shared_formal_completion_by_full_cid(self):
        fixture = FIXTURES['amd']
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            attempt = guard.HardwareAttempt.__new__(guard.HardwareAttempt)
            attempt.fixture = fixture
            attempt.out = attempt.base = out
            service = Mock()
            service._creation_lock.return_value = nullcontext()
            spec = guard.fixture_create_spec(fixture)
            service._prepare_create_locked.return_value = (spec, 'target', 'relationship', None)
            record = {'test': 'formal'}
            service._finish_create.return_value = record
            attempt.service = lambda: service
            attempt.check_preconditions = lambda: {}
            attempt.verify_official_binding = Mock()
            reuse.put(out/'non-target-before.json', {})
            with patch.object(guard, 'verify_hardware_permission'):
                attempt.begin_amd_bootstrap_recover()
            service._finish_create.assert_called_once_with(
                spec, fixture.env_id, 'target', 'relationship', None,
                expected_container_id=guard.AMD_BOOTSTRAP_CID)
            service.create.assert_not_called()
            service.runtime.remove.assert_not_called()
            attempt.verify_official_binding.assert_called_once_with(record)
            marker = reuse.get(out/guard.AMD_BOOTSTRAP_MARKER)
            self.assertEqual(marker['cid'], guard.AMD_BOOTSTRAP_CID)


class AmdDnfBudgetTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.cid = guard.AMD_BOOTSTRAP_CID
        self.aggregate = '/aggregate'
        self.group = self.aggregate+'/libpod-'+self.cid+'.scope'
        self.scope = {'Id': 'libpod-'+self.cid+'.scope', 'ControlGroup': self.group,
                      'InvocationID': 'test-scope'}
        self.init = {'pid': 20, 'starttime': '11', 'boot_id': 'boot', 'cgroup': self.group}
        self.before = {'memory.max': 384*guard.MIB, 'memory.swap.max': 128*guard.MIB, 'pids.max': 128,
                       'cpu.max': {'quota': 100000, 'period': 100000}, 'chain': [
                           {'cgroup': self.group, 'limits': {
                               'memory.max': str(384*guard.MIB), 'memory.swap.max': str(128*guard.MIB),
                               'pids.max': '128', 'cpu.max': '100000 100000'}},
                           {'cgroup': self.aggregate, 'limits': {
                               'memory.max': str(1024*guard.MIB), 'memory.swap.max': str(128*guard.MIB),
                               'pids.max': '256', 'cpu.max': '100000 100000'}}]}
        self.after = amd_dnf_effective_limits(self.before, self.group)
        self.live = {'cid': self.cid, 'init': self.init, 'scope': self.scope, 'coverage_verified': True,
                     'started_at': 'approved', 'guest_limits': self.before}
        self.attempt = guard.HardwareAttempt.__new__(guard.HardwareAttempt)
        self.attempt.out = self.attempt.base = self.out
        self.attempt.spec = {'guest_profile': guard.AMD_DNF_PROFILE}
        self.attempt.resources = {'memory': 384*guard.MIB, 'memory_swap': 512*guard.MIB}
        self.attempt.active_config = lambda: {'cid': self.cid}
        self.owner = {**self.init, 'pid': 21, 'cgroup': self.aggregate+'/worker'}
        reuse.put(self.out/guard.AMD_DNF_RETRY_MARKER, {'attempt': str(self.out),
            'binding': guard.AMD_DNF_BINDING, 'owner': self.owner})
        reuse.put(self.out/'ready.json', {'aggregate': self.aggregate})

    def test_amended_profile_is_only_for_the_exact_amd_query(self):
        valid = create_spec(fixture='amd', phase='prepare', supervisor_tasks=20, supervisor_memory_mb=96,
                            acl_guard_unit='unifier-v008-amd-acl-dnf-retry.service',
                            command=guard.AMD_DNF_RETRY_COMMAND, guest_profile=guard.AMD_DNF_PROFILE)
        self.assertIs(guard.validate_spec(valid), FIXTURES['amd'])
        for changes in ({'phase': 'create'}, {'fixture': 'intel'}, {'guest_profile': '999MiB'},
                        {'command': guard.AMD_DNF_RETRY_COMMAND + ['extra']},
                        {'package_plan': {}}, {'supervisor_memory_mb': 48}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                guard.validate_spec({**valid, **changes})

    def test_transient_update_and_subsequent_binding_preserve_other_limits(self):
        self.assertEqual(self.before['memory.max'], 384*guard.MIB)
        self.assertEqual(self.after['memory.max'], 768*guard.MIB)
        self.assertEqual(self.before['chain'][1], self.after['chain'][1])
        self.assertEqual(self.before['memory.swap.max'], self.after['memory.swap.max'])
        (self.out/'memory.max').write_text(str(384*guard.MIB))
        (self.out/'memory.high').write_text('max')
        with patch.object(guard, 'verify_hardware_permission'), \
             patch.object(reuse, 'unit', return_value=self.scope), \
             patch.object(reuse, 'process', side_effect=[self.owner, self.init, self.init, self.init]), \
             patch.object(reuse, 'effective_limits', side_effect=[self.before, self.after]), \
             patch.object(reuse, 'cgroup_dir', return_value=self.out), \
             patch.object(reuse, 'info', return_value={'State': {'StartedAt': 'approved'}}):
            self.attempt.apply_amd_dnf_budget(self.live)
        self.assertEqual((self.out/'memory.max').read_text(), str(768*guard.MIB))
        self.assertEqual((self.out/'memory.high').read_text(), str(640*guard.MIB))
        self.assertEqual(self.attempt.expected_live_memory(self.live, new_start=True), 384*guard.MIB)
        self.assertEqual(self.attempt.expected_live_memory(self.live, new_start=False), 768*guard.MIB)
        with patch.object(reuse, 'cgroup_dir', return_value=self.out), \
             patch.object(reuse, 'effective_limits', return_value={**self.after, 'pids.max': 129}):
            with self.assertRaisesRegex(RuntimeError, 'active limits changed'):
                self.attempt.verify_live_guest_limits(self.live, {**self.after, 'pids.max': 129},
                                                      self.aggregate, guard.time.monotonic())
        with self.assertRaisesRegex(RuntimeError, 'not tied'):
            other = {**self.live, 'scope': {**self.scope, 'InvocationID': 'replacement'}}
            self.attempt.expected_live_memory(other, new_start=False)

    def test_refuses_cgroup_without_unique_old_limit_or_with_tighter_ancestor(self):
        with self.assertRaisesRegex(RuntimeError, 'not unique'):
            amd_dnf_effective_limits(self.before, self.group+'/unverified')
        chain = [self.before['chain'][0], {'cgroup': self.aggregate, 'limits': {'memory.max': str(600*guard.MIB)}}]
        with self.assertRaisesRegex(RuntimeError, 'another cgroup'):
            amd_dnf_effective_limits({**self.before, 'chain': chain}, self.group)

    def test_guest_abort_records_the_exact_triggering_cgroup_counters(self):
        self.attempt.fixture = FIXTURES['amd']
        self.attempt.phase = 'prepare'
        self.attempt.spec['seconds'] = 120
        self.attempt.guest_policy = None
        self.attempt.config = {'reserve_bytes': 1024*guard.MIB}
        self.attempt.check_acl_watch = Mock()
        self.attempt.check_disk = Mock()
        device = {'rdev': 57984}
        reuse.put(self.out/'device-before.json', device)
        reuse.put(self.out/'live-binding.json', {**self.live, 'conmon': self.owner})
        for name in ('command.log', 'events.jsonl', 'events.stderr', 'samples.jsonl'):
            (self.out/name).touch()
        metrics = {'memory': 380*guard.MIB, 'tasks': 11, 'events': {'max': 8, 'oom': 0, 'oom_kill': 0},
                   'pids_events': {'max': 0}}
        with patch.object(guard, 'observe_device', return_value=device), \
             patch.object(reuse, 'process', side_effect=(self.init, self.owner)), \
             patch.object(reuse, 'effective_limits', return_value=self.before), \
             patch.object(reuse, 'load_metrics', return_value=metrics):
            with self.assertRaisesRegex(RuntimeError, 'GUEST_INVALID_HARD_LIMIT'):
                self.attempt.sample(50)
        sample = reuse.get(self.out/'guest-limit-sample.json')
        self.assertEqual(sample['cid'], self.cid)
        self.assertEqual(sample['init'], self.init)
        self.assertEqual(sample['events'], metrics['events'])
        self.assertEqual(sample['guest_limits'], self.before)
        self.assertEqual(sample['observed_monotonic'], 50)
        self.assertEqual(sample['reason'], 'INVALID_HARD_LIMIT')


class IntelResumeTests(TestCase):
    def test_resume_is_pinned_to_only_the_approved_intel_attempt(self):
        spec = create_spec(phase='resume', resume_from=str(guard.INTEL_RESUME_FROM))
        self.assertIs(guard.validate_spec(spec), INTEL)
        locale_retry = {**spec, 'retry_from': str(guard.INTEL_RETRY_FROM)}
        self.assertIs(guard.validate_spec(locale_retry), INTEL)
        self.assertIs(guard.validate_spec({**locale_retry,
            'validator_retry_from': str(guard.INTEL_VALIDATOR_RETRY_FROM)}), INTEL)
        for change in ({'resume_from': '/tmp/older'}, {'fixture': 'amd',
                        'acl_guard_unit': 'unifier-v008-amd-acl-resume.service'},
                       {'command': ['/usr/bin/python3', 'arbitrary.py']}, {'phase': 'create'},
                       {'retry_from': '/tmp/unrelated'},
                       {'validator_retry_from': str(guard.INTEL_VALIDATOR_RETRY_FROM)}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.validate_spec({**spec, **change})

    def test_resume_requires_unchanged_old_evidence_and_never_started_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original = base/'original'
            original.mkdir(mode=0o700)
            fixture = replace(INTEL, root=base/'environment')
            attempt = guard.HardwareAttempt.__new__(guard.HardwareAttempt)
            attempt.fixture, attempt.base = fixture, base
            attempt.spec = create_spec(phase='resume', resume_from=str(original))
            attempt.hooks = base/'empty-hooks'
            attempt.once = base/'create-attempt.json'
            attempt.binding_path = base/'fixture-binding.json'
            attempt.record_path = base/'official-record.json'
            cid = guard.INTEL_RESUME_CID
            owner = {'pid': 101, 'starttime': '2', 'boot_id': 'test-boot', 'cgroup': '/worker'}
            create_event = {'ID': cid, 'Name': fixture.name, 'Image': fixture.reference,
                            'Status': 'create', 'Type': 'container', 'Attributes': fixture.labels,
                            'timeNano': 120}
            command = {'at': 1, 'exit_code': 2}
            before = {OTHER: {'name': 'old', 'state': {'Running': False}}}
            untouched = {'Status': 'created', 'Running': False, 'Pid': 0,
                         'StartedAt': '0001-01-01T00:00:00Z', 'FinishedAt': '0001-01-01T00:00:00Z',
                         'ExitCode': 0, 'OOMKilled': False, 'Restarting': False}
            after = {**before, cid: {'name': fixture.name, 'labels': fixture.labels, 'state': untouched}}
            reuse.put(attempt.once, {'attempt': str(original), 'fixture': 'intel', 'owner': owner})
            evidence = {
                'spec.json': create_spec(), 'command-identity.json': owner,
                'create-intent.json': {'at_ns': 100, 'argv': guard.fixture_create_argv(
                    fixture, attempt.hooks, guard.fixture_create_spec(fixture))},
                'create-event.json': create_event, 'created-container.json': {'cid': cid},
                'command-result.json': command,
                'receipt.json': {'reason': None, 'command_result': command, 'cleanup': {'status': 'NO_START'}},
                'rescue.json': {'status': 'NO_START'},
                'non-target-before.json': before, 'native-after.json': after,
            }
            for name, payload in evidence.items():
                reuse.put(original/name, payload)
            (original/'events.jsonl').write_text(guard.json.dumps(create_event)+'\n')
            (original/'command.log').write_text('container identity or image differs from the approved fixture\n')
            with patch.object(guard, 'INTEL_RESUME_FROM', original), patch.dict(guard.FIXTURES, intel=fixture):
                verified = attempt.verify_resume_provenance(after)
                self.assertEqual(verified['cid'], cid)
                for changed in ({**after, OTHER: {'name': 'old', 'state': {'Running': True}}},
                                {**after, cid: {**after[cid], 'state': {**untouched, 'Status': 'exited'}}}):
                    with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, 'state differs'):
                        attempt.verify_resume_provenance(changed)
                (original/'abort.json').touch()
                with self.assertRaisesRegex(RuntimeError, 'abort.json'):
                    attempt.verify_resume_provenance(after)
                (original/'abort.json').unlink()
                failed = base/'failed-resume'
                failed.mkdir(mode=0o700)
                reuse.put(base/'resume-attempt.json', {'attempt': str(failed),
                    'original_attempt': str(original), 'cid': cid, 'owner': owner})
                retry_evidence = {
                    'spec.json': attempt.spec, 'command-identity.json': owner,
                    'resume-proof.json': verified, 'command-result.json': command,
                    'receipt.json': evidence['receipt.json'], 'rescue.json': {'status': 'NO_START'},
                    'start-intent.json': {'cid': cid, 'at_ns': 150, 'before': {**untouched, 'Error': ''}},
                    'non-target-before.json': after, 'native-after.json': after,
                }
                for name, payload in retry_evidence.items():
                    reuse.put(failed/name, payload)
                (failed/'command.log').write_text(
                    'podman start failed: conmon: option parsing failed: Invalid byte sequence in conversion input\n'
                    f'Error: unable to start container "{cid}": {guard.INTEL_RETRY_START_ERROR}\n')
                (failed/'events.jsonl').touch()
                attempt.spec = {**attempt.spec, 'retry_from': str(failed)}
                with patch.object(guard, 'INTEL_RETRY_FROM', failed):
                    proof = attempt.verify_resume_provenance(after)
                    self.assertEqual(proof['locale_retry']['expected_error'], guard.INTEL_RETRY_START_ERROR)
                    (failed/'events.jsonl').write_text(guard.json.dumps({'Status': 'start', 'ID': cid})+'\n')
                    with self.assertRaisesRegex(RuntimeError, 'no-start conmon'):
                        attempt.verify_resume_provenance(after)
                    (failed/'events.jsonl').write_text('')
                    (base/'resume-locale-retry-attempt.json').touch()
                    with self.assertRaisesRegex(RuntimeError, 'resume-locale-retry-attempt.json'):
                        attempt.verify_resume_provenance(after)
                    validator_failed = base/'failed-validator'
                    validator_failed.mkdir(mode=0o700)
                    reuse.put(base/'resume-locale-retry-attempt.json', {'attempt': str(validator_failed),
                        'original_attempt': str(original), 'cid': cid, 'owner': owner})
                    cleanup = {'status': 'NO_START', 'worker_final': {'MainPID': '0', 'ControlGroup': ''}}
                    validator_evidence = {
                        'spec.json': attempt.spec, 'command-identity.json': owner,
                        'resume-proof.json': proof, 'command-result.json': command,
                        'receipt.json': {**evidence['receipt.json'], 'cleanup': cleanup},
                        'cleanup.json': cleanup, 'rescue.json': cleanup,
                        'non-target-before.json': after, 'native-after.json': after,
                    }
                    for name, payload in validator_evidence.items():
                        reuse.put(validator_failed/name, payload)
                    (validator_failed/'command.log').write_text(
                        'resume-create environment differs from the cached image\n')
                    (validator_failed/'events.jsonl').touch()
                    attempt.spec = {**attempt.spec, 'validator_retry_from': str(validator_failed)}
                    with patch.object(guard, 'INTEL_VALIDATOR_RETRY_FROM', validator_failed):
                        result = attempt.verify_resume_provenance(after)
                        self.assertEqual(result['validator_retry']['attempt'], str(validator_failed))
                        (validator_failed/'start-intent.json').touch()
                        with self.assertRaisesRegex(RuntimeError, 'start-intent.json'):
                            attempt.verify_resume_provenance(after)
                        (validator_failed/'start-intent.json').unlink()
                        (validator_failed/'command.log').write_text('different validation failure\n')
                        with self.assertRaisesRegex(RuntimeError, 'recorded pre-start environment failure'):
                            attempt.verify_resume_provenance(after)


class IntelPackageMaintenanceTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.attempt = guard.HardwareAttempt.__new__(guard.HardwareAttempt)
        self.attempt.fixture = INTEL
        self.attempt.out = self.out
        self.attempt.base = self.out
        self.cid = guard.INTEL_RESUME_CID

    def test_exact_two_phase_inputs(self):
        common = create_spec(recover_from=str(guard.INTEL_RECOVER_FROM),
                             transaction=guard.INTEL_RECOVER_TRANSACTION)
        repair = {**common, 'phase': 'native-repair',
                  'command': [guard.PYTHON, str(guard.INTEL_REPAIR_CONTROLLER)]}
        recovery = {**common, 'phase': 'recover', 'repair_from': str(guard.INTEL_REPAIR_FROM)}
        self.assertIs(guard.validate_spec(repair), INTEL)
        self.assertIs(guard.validate_spec(recovery), INTEL)
        for candidate in ({**repair, 'transaction': 'txn123456789abc'},
                          {**repair, 'command': [guard.PYTHON, '/tmp/other.py']},
                          {**recovery, 'repair_from': '/tmp/not-reviewed'},
                          {**recovery, 'seconds': 121},
                          {**recovery, 'command': repair['command']},
                          {**recovery, 'fixture': 'amd',
                           'acl_guard_unit': 'unifier-v008-amd-acl-fixture-test.service'}):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                guard.validate_spec(candidate)

    def test_prepare_gate_rejects_pending_even_before_record_writes_recovery_flag(self):
        for record, pending in (({}, [{'phase': 'running'}]), ({'package_recovery_required': True}, [])):
            self.attempt.pending_transactions = lambda: (record, pending)
            with self.subTest(record=record, pending=pending), self.assertRaisesRegex(RuntimeError, 'formal package recovery'):
                self.attempt.require_clean_packages()

    def test_repair_only_submits_verified_archive_and_exact_sequence(self):
        config = {'cid': self.cid}
        runtime = guard.HardwareRuntime(self.attempt)
        options = {'interactive': False, 'stream': False, 'env': guard.intel_repair_environment(),
                   'on_guest': lambda _: None}
        verified = {'path': guard.INTEL_REPAIR_ARCHIVE, 'size': guard.INTEL_REPAIR_SIZE,
                    'sha256': guard.INTEL_REPAIR_SHA256}
        cache = [{'name': f'p-{i}', 'version': '1.0', 'arch': 'amd64',
                  'path': f'/var/cache/apt/archives/p-{i}_1.0_amd64.deb',
                  'size': 1, 'sha256': 'a'*64} for i in range(53)]
        self.attempt.repair_cache = lambda: cache
        cache_report = {'package_count': 53, 'total_bytes': 70470836,
                        'entries': [{**entry, 'stat': '81a4 1 2 1 100 100'} for entry in cache]}
        path = self.out/'native-maintenance.json'
        reuse.put(path, {'verified_archive': {**verified, 'size': 1}})
        unpack, install, audit = guard.INTEL_REPAIR_STEPS.values()
        with self.assertRaisesRegex(RuntimeError, 'checked cache'):
            runtime.admit_native_repair_step(config, unpack, options)
        reuse.put(path, {'verified_archive': verified})
        with self.assertRaisesRegex(RuntimeError, '53-package plan'):
            runtime.admit_native_repair_step(config, unpack, options)
        reuse.put(path, {'verified_archive': verified, 'verified_closure': cache_report})
        with self.assertRaisesRegex(RuntimeError, 'single-thread'):
            runtime.admit_native_repair_step(config, unpack, {**options, 'env': {}})
        with self.assertRaisesRegex(RuntimeError, 'sequence'):
            runtime.admit_native_repair_step(config, install, options)
        runtime.admit_native_repair_step(config, unpack, options)
        with self.assertRaisesRegex(RuntimeError, 'sequence'):
            runtime.admit_native_repair_step(config, unpack, options)
        runtime.admit_native_repair_step(config, install, options)
        runtime.admit_native_repair_step(config, audit, options)
        with self.assertRaisesRegex(RuntimeError, 'already been submitted'):
            runtime.admit_native_repair_step(config, audit, options)

    def test_native_maintenance_only_accepts_the_official_stop_stamp(self):
        before = {'phase': 'running', 'native': [{'exit_code': 137}]}
        after = {**before, 'container_stopped_at': '2026-11-15T20:00:00+08:00'}
        self.assertTrue(guard.same_pending_transaction_after_stop(before, after))
        self.assertFalse(guard.same_pending_transaction_after_stop(before, before))
        self.assertFalse(guard.same_pending_transaction_after_stop(before, {**after, 'phase': 'failed'}))

    def test_readonly_reconciliation_covers_original_sources_and_exact_optional_package(self):
        previous = self.out/'old-repair'
        previous.mkdir()
        output = self.out/'readonly-reconciliation.json'
        reported = {'verified_closure': {'package_count': 53},
                    'transaction_after': {'phase': 'running'}}
        with patch.object(guard, 'INTEL_REPAIR_PLAN', self.out/'original-plan.json'), \
             patch.object(guard, 'INTEL_RECOVER_FROM', self.out), \
             patch.object(guard, 'INTEL_RECONCILIATION', output):
            sources = []
            for path in guard.intel_reconciliation_sources(previous):
                payload = ('evidence for '+path.name).encode()
                path.write_bytes(payload)
                sources.append({'path': str(path), 'bytes': len(payload),
                                'sha256': hashlib.sha256(payload).hexdigest()})
            self.assertEqual(len(sources), 23)
            inventory_after = previous/'maintenance-07-inventory-after.stdout'
            proof = {'schema_version': 1, 'kind': 'readonly-package-reconciliation',
                     'status': 'RECONCILED_REQUIRES_PKG_RECOVER', 'source_attempt': str(previous),
                     'binding': guard.INTEL_RECOVER_BINDING, 'transaction_id': guard.INTEL_RECOVER_TRANSACTION,
                     'package_recovery_completed': False, 'original_exact_requests': guard.INTEL_REPAIR_REQUESTS,
                     **reported, 'installed_original_closure': 52,
                     'omitted_recommendation': guard.INTEL_OPTIONAL_RECOMMENDATION,
                     'inventory_after_sha256': hashlib.sha256(inventory_after.read_bytes()).hexdigest(),
                     'source_files': sources}
            reuse.put(output, proof)
            self.assertEqual(self.attempt.verify_intel_reconciliation(previous, reported)['installed_original_closure'], 52)
            altered_package = {**guard.INTEL_OPTIONAL_RECOMMENDATION, 'name': 'libllvm19'}
            reuse.put(output, {**proof, 'omitted_recommendation': altered_package})
            with self.assertRaisesRegex(RuntimeError, 'optional omission'):
                self.attempt.verify_intel_reconciliation(previous, reported)
            reuse.put(output, proof)
            (previous/'rescue.json').write_text('altered')
            with self.assertRaisesRegex(RuntimeError, 'fingerprint mismatch'):
                self.attempt.verify_intel_reconciliation(previous, reported)

    def test_actual_intel_reconciliation_binds_install_stage_package_metadata(self):
        if not guard.INTEL_RECONCILIATION.is_file():
            self.skipTest('Intel reconciliation artifacts are available only in the original workspace')
        repair = guard.INTEL_REPAIR_FROM
        report = guard.bound_read(repair/'native-maintenance.json')
        proof = self.attempt.verify_intel_reconciliation(repair, report)
        self.assertEqual(proof['installed_original_closure'], 52)
        self.assertEqual(proof['omitted_recommendation'], guard.INTEL_OPTIONAL_RECOMMENDATION)

    def test_stopped_inventory_command_cannot_redirect_helper_or_record(self):
        runtime = guard.HardwareRuntime(self.attempt)
        record = {'id': 'debian13', 'target': 'debian13', 'schema_version': 3,
                  'environment_uuid': guard.INTEL_RECOVER_BINDING['environment_uuid'], 'generation': 1,
                  'runtime': {'container_id': self.cid, 'project': str(INTEL.root)}}
        runtime.inventory_record = record
        args = ['podman', 'unshare', sys.executable, str(guard.ROOT/'src/unifier/inventory_probe.py'),
                '--record', guard.json.dumps(record), '--journal', str(self.attempt.storage_observation_path()),
                '--token', 'a'*32]
        runtime.verify_inventory_command(args)
        for index, value in ((3, '/tmp/inventory_probe.py'), (7, '/tmp/journal.json'), (9, 'invalid'),
                             (5, guard.json.dumps({**record, 'generation': 2}))):
            changed = list(args)
            changed[index] = value
            with self.subTest(index=index), self.assertRaisesRegex(RuntimeError, 'outside'):
                runtime.verify_inventory_command(changed)

    def test_memory_high_only_tightens_existing_bound_child(self):
        self.attempt.phase, self.attempt.spec = 'native-repair', {}
        init = {'pid': 10, 'starttime': '2', 'boot_id': 'b', 'cgroup': '/aggregate/guest'}
        binding = {'cid': self.cid, 'coverage_verified': True, 'init': init,
                   'guest_limits': {'memory.max': 384*guard.MIB}}
        high = self.out/'memory.high'
        hard = self.out/'memory.max'
        high.write_text('max')
        hard.write_text(str(384*guard.MIB))
        with patch.object(guard, 'verify_hardware_permission'), \
             patch.object(reuse, 'process', return_value=init), \
             patch.object(reuse, 'cgroup_dir', return_value=self.out):
            self.attempt.set_package_memory_high(binding)
            self.assertEqual(high.read_text(), str(256*guard.MIB))
            self.assertEqual(hard.read_text(), str(384*guard.MIB))
            high.write_text(str(128*guard.MIB))
            with self.assertRaisesRegex(RuntimeError, 'cannot raise'):
                self.attempt.set_package_memory_high(binding)


class AmdOfflineInstallTests(TestCase):
    evidence = (Path(__file__).resolve().parents[1]
                /'artifacts/v008-gpu-drm-20261115T183930+0800')

    def require_archived_evidence(self):
        if not self.evidence.is_dir():
            self.skipTest('repository-local AMD hardware receipts are outside the source bundle')

    def test_offline_install_submission_requires_150_seconds_to_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            attempt = SimpleNamespace(out=Path(directory), check_acl_watch=Mock())
            deadline = attempt.out/'ready.json'
            freshness = attempt.out/'amd-offline-install-before.json'
            with patch.object(guard.time, 'monotonic', return_value=1000), \
                 patch.object(guard.time, 'time', return_value=2000):
                reuse.put(deadline, {'phase_deadline': 1150})
                reuse.put(freshness, {'valid_until_unix': 2150})
                guard.amd_install_submission_time(attempt)
                attempt.check_acl_watch.assert_called_once_with(150+guard.ACL_QUEUE_RESERVE)
                for remaining_phase, remaining_authority in ((1149, 2150), (1150, 2149)):
                    reuse.put(deadline, {'phase_deadline': remaining_phase})
                    reuse.put(freshness, {'valid_until_unix': remaining_authority})
                    with self.assertRaisesRegex(RuntimeError, 'insufficient remaining'):
                        guard.amd_install_submission_time(attempt)

    def test_install_profile_accepts_only_the_fixed_command_and_plan(self):
        spec = create_spec(fixture='amd', phase='prepare', seconds=180,
                           supervisor_tasks=20, supervisor_memory_mb=96,
                           acl_guard_unit='unifier-v008-amd-acl-dnf-install.service',
                           command=guard.AMD_DNF_INSTALL_COMMAND,
                           package_plan=guard.AMD_DNF_EXACT_PLAN,
                           guest_profile=guard.AMD_DNF_INSTALL_PROFILE)
        self.assertIs(guard.validate_spec(spec), FIXTURES['amd'])
        for key, value in (('command', guard.AMD_DNF_RETRY_COMMAND),
                           ('seconds', 120), ('package_plan', {}),
                           ('guest_profile', guard.AMD_DNF_PROFILE)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                guard.validate_spec({**spec, key: value})
        with self.assertRaisesRegex(ValueError, 'reviewed offline installation'):
            guard.validate_spec({key: value for key, value in spec.items() if key != 'guest_profile'})

    def test_real_primary_and_query_bind_fresh_prefetch_and_all_28_rpms(self):
        self.require_archived_evidence()
        now = 1794749000
        query_raw = (self.evidence/'server/amd-query-memory/result.json').read_bytes()
        query = guard.json.loads(query_raw)
        query_digest = hashlib.sha256(query_raw).hexdigest()
        primary = (self.evidence/'fedora-primary-inputs/primary-checksums.json').read_bytes()
        stage_digest = query['stage_marker_sha256']
        repos = {repo: {'completed_unix': now-50} for repo in
                 ('fedora', 'updates', 'fedora-cisco-openh264')}
        links = guard.json.dumps({'schema': 1, 'latest': False,
                                  'repos': repos, 'valid_until_unix': now+1750}).encode()
        prefetch = {'schema': 1, 'status': 'VERIFIED', 'cid': guard.AMD_BOOTSTRAP_CID,
                    'environment_uuid': guard.AMD_DNF_BINDING['environment_uuid'], 'generation': 1,
                    'query_sha256': query_digest, 'stage_marker_sha256': stage_digest,
                    'primary_manifest_sha256': hashlib.sha256(primary).hexdigest(),
                    'metalink_receipt_sha256': hashlib.sha256(links).hexdigest(),
                    'valid_until_unix': now+1750, 'official': repos, 'latest': False,
                    'formal_stop': 'NO_START', 'payload_root': str(guard.AMD_DNF_PAYLOAD),
                    'package_plan': guard.AMD_DNF_EXACT_PLAN, 'budget': {},
                    'package_signatures': 'pending-native-gpg-verification',
                    'scoped_for': 'amd-fedora44-prefetch'}
        packages = []
        for row in guard.json.loads(primary)['packages']:
            repo, relative = row['repository'], row['location']
            package = {key: value for key, value in row.items() if key != 'dnf_location'}
            package.update(file=str(guard.AMD_DNF_PAYLOAD/repo/Path(relative).name),
                           url=guard.AMD_DNF_MIRRORS[repo]+relative,
                           transport={'completed_unix': now-10, 'size_download': row['download_bytes'],
                                      'size_header': 1024})
            packages.append(package)

        def validate(report, at, **options):
            return guard.validate_amd_package_receipts(
                query, query_digest, primary, guard.json.dumps(report).encode(), links, at, **options)

        prefetch['packages'] = packages
        proof = validate(prefetch, now)
        self.assertEqual(len(proof['packages']), 28)
        self.assertEqual(sum(row['download_bytes'] for row in proof['packages']), 67971267)
        for patch_row in ({'sha256': '0'*64}, {'file': '/tmp/untrusted.rpm'},
                          {'url': 'https://not-approved.invalid/a.rpm'}):
            with self.subTest(keys=patch_row), self.assertRaisesRegex(RuntimeError, 'RPM differs'):
                validate({**prefetch, 'packages': [{**packages[0], **patch_row}, *packages[1:]]}, now)
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            validate(prefetch, now+1700)
        historical = validate(prefetch, now+1800, historical_at=now)
        self.assertEqual(historical['valid_until_unix'], prefetch['valid_until_unix'])
        self.assertNotIn('refresh_sha256', historical)
        with self.assertRaisesRegex(RuntimeError, 'archived prefetch'):
            validate(prefetch, now+1800, historical_at=now+1800)
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            validate({**prefetch, 'metalink_receipt_sha256': '0'*64}, now)

    def test_refresh_requires_new_live_authority_without_changing_historical_payload(self):
        self.require_archived_evidence()
        root = self.evidence/'server'
        past = guard.json.loads((root/'amd-package-install-retry/amd-offline-install-before.json').read_bytes())
        old = guard.json.loads((root/'fedora-package-prefetch-continuation/result.json').read_bytes())
        cutoff = past['valid_until_unix']
        now = cutoff+25
        repos = {'fedora': {'completed_unix': cutoff+15, 'size_download': 200}}
        link = {'schema': 1, 'repos': repos, 'latest': False, 'valid_until_unix': cutoff+1215}
        link_raw = guard.json.dumps(link).encode()
        names = ('result.json', 'receipt.json', 'cleanup.json', 'command-result.json',
                 'native-after.json', 'amd-offline-install-before.json')
        reserved_download, reserved_write, retry_write = 1966080, 4063232, 2097152
        old_budget = past['budget']
        budget = {**old_budget,
                  'previous_reserved_download_bytes': old_budget['reserved_download_bytes'],
                  'previous_reserved_write_bytes': old_budget['reserved_write_bytes'],
                  'metadata_refresh_reserved_download_bytes': reserved_download,
                  'metadata_refresh_reserved_write_bytes': reserved_write,
                  'metadata_refresh_rpm_download_bytes': 0,
                  'prior_retry_evidence_reserved_write_bytes': retry_write,
                  'metadata_refresh_payload_bytes': 200, 'metadata_refresh_response_header_bytes': 80,
                  'reserved_download_bytes': old_budget['reserved_download_bytes']+reserved_download,
                  'reserved_write_bytes': old_budget['reserved_write_bytes']+reserved_write+retry_write,
                  'future_native_payload_and_install_estimate':
                      old_budget['future_native_payload_and_install_estimate']+reserved_write+retry_write,
                  'measured_download_with_response_headers': old_budget['measured_download_with_response_headers']+280}
        report = {'schema': 1, 'status': 'VERIFIED', 'formal_stop': 'NO_START', 'latest': False,
                  'cid': guard.AMD_BOOTSTRAP_CID, 'environment_uuid': guard.AMD_DNF_BINDING['environment_uuid'],
                  'generation': 1, 'prefetch_result_sha256': past['prefetch_sha256'],
                  'historical_metalink_sha256': past['metalink_sha256'], 'query_sha256': past['query_sha256'],
                  'primary_manifest_sha256': past['primary_sha256'], 'stage_marker_sha256': past['stage_marker_sha256'],
                  'metalink_receipt_sha256': hashlib.sha256(link_raw).hexdigest(), 'official': repos,
                  'package_plan': guard.AMD_DNF_EXACT_PLAN, 'packages': old['packages'],
                  'rpm_download_bytes': 0, 'package_signatures': 'pending-native-gpg-verification',
                  'valid_until_unix': link['valid_until_unix'], 'budget': budget,
                  'prior_install_retry': {'out': str(guard.AMD_DNF_INSTALL_RETRY_FROM), 'status': 'FAIL',
                                          'no_submission': True, 'sha256': {
                                              name: guard.AMD_DNF_INSTALL_RETRY_FAILURE_HASHES[name]
                                              for name in names}}}
        validate = guard.validate_amd_refresh_receipt
        self.assertEqual(validate(report, link, link_raw, past, old['packages'], cutoff, now),
                         cutoff+1215)
        with self.assertRaisesRegex(RuntimeError, 'current independent'):
            validate(report, link, link_raw, past, old['packages'], cutoff, cutoff+1066)
        with self.assertRaisesRegex(RuntimeError, 'same payload'):
            validate({**report, 'packages': old['packages'][:-1]}, link, link_raw, past,
                     old['packages'], cutoff, now)

    def test_payload_signature_requires_the_full_trusted_fingerprint_and_digest(self):
        self.require_archived_evidence()
        import runpy

        controller = runpy.run_path(str(self.evidence/'fedora-install-controller.py'))
        verify = controller['verify_signatures']
        path = '/cache/example.rpm'
        fingerprint = guard.AMD_DNF_SIGNING_FINGERPRINT
        evidence = (f'{path}:\n    Header OpenPGP V4 RSA/SHA512 signature, key fingerprint: {fingerprint}: OK\n'
                    '    Header SHA256 digest: OK\n    Payload SHA256 digest: OK\n')
        verify(evidence, [path])
        for invalid in (evidence.replace(fingerprint, 'a'*40),
                        evidence.replace('Payload SHA256 digest: OK', 'Payload SHA256 digest: BAD'),
                        evidence.replace(f'key fingerprint: {fingerprint}: OK', 'NOTFOUND')):
            with self.assertRaisesRegex(RuntimeError, 'trusted key'):
                verify(invalid, [path])

    def test_cache_hardlink_accounting_adjusts_only_unreserved_existing_bytes(self):
        self.require_archived_evidence()
        import runpy

        controller = runpy.run_path(str(self.evidence/'fedora-install-controller.py'))
        allocate = controller['write_budget']
        prefetch = {'reserved_write_bytes': 300*guard.MIB, 'prior_write_bytes': 200*guard.MIB,
                    'measured_download_with_response_headers': 120*guard.MIB}
        approved = allocate(prefetch, {'/old': 80*guard.MIB, '/new': 70*guard.MIB})
        self.assertEqual(approved['prior_cache_accounting_adjustment_bytes'], 0)
        increased = allocate(prefetch, {'/old': 140*guard.MIB, '/new': 90*guard.MIB})
        self.assertEqual(increased['prior_cache_accounting_adjustment_bytes'],
                         49_794_425+30*guard.MIB-67_971_267)
        self.assertEqual(increased['new_guest_rpm_bytes'], 0)
        with self.assertRaisesRegex(RuntimeError, 'cumulative disk'):
            allocate({**prefetch, 'reserved_write_bytes': 900*guard.MIB},
                     {'/old': 90*guard.MIB, '/new': 70*guard.MIB})

    def test_retry_native_provenance_is_only_the_exact_recorded_option_exit(self):
        self.require_archived_evidence()
        txn = guard.json.loads((self.evidence/'server/amd-package-install/package-transaction.json').read_bytes())
        guard.validate_amd_dnf_option_failure(txn)
        for changes in ({'exit_code': 1}, {'stdout_tail': 'Installing packages'},
                        {'stderr_tail': 'different error'}):
            with self.assertRaisesRegex(RuntimeError, 'exact unsupported'):
                guard.validate_amd_dnf_option_failure({**txn, 'native': [{**txn['native'][0], **changes}]})
        self.assertIn('--setopt=gpgcheck=1', guard.AMD_DNF_OFFLINE_OPTIONS)

    def test_formal_native_step_forces_exact_offline_flags_and_cannot_be_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            plan = guard.AMD_DNF_EXACT_PLAN
            fixture = FIXTURES['amd']
            cfg = {'cid': guard.AMD_BOOTSTRAP_CID}
            attempt = SimpleNamespace(phase='prepare', out=out, base=out, fixture=fixture,
                                      spec={'guest_profile': guard.AMD_DNF_INSTALL_PROFILE,
                                            'fixture': 'amd', 'package_plan': plan},
                                      active_config=lambda: cfg)
            owner = {'pid': 23, 'starttime': '123', 'boot_id': 'boot', 'cgroup': '/test/controller'}
            archived = {'package_plan': plan, 'closure': []}
            before = {'query_sha256': 'a'*64, 'prefetch_sha256': 'b'*64, 'packages': [{'rpm': 'expected'}]}
            reuse.put(out/'amd-offline-install-before.json', before)
            reuse.put(out/'amd-offline-install-ready.json', {**before, 'binding': guard.AMD_DNF_BINDING,
                       'signing_fingerprint': guard.AMD_DNF_SIGNING_FINGERPRINT,
                       'solver_plan': archived, 'verified_rpms': before['packages']})
            reuse.put(out/guard.AMD_DNF_INSTALL_MARKER, {'attempt': str(out),
                       'binding': guard.AMD_DNF_BINDING, 'prefetch_sha256': before['prefetch_sha256'],
                       'owner': owner})
            approved_query = out/'evidence'
            approved_query.mkdir()
            reuse.put(approved_query/'result.json', {'plan': archived})
            runtime = guard.HardwareRuntime(attempt)
            logical = ['dnf', 'install', '-y', *guard.package_names('amd', plan['requests'])]
            pinned = ['dnf', *guard.AMD_DNF_OFFLINE_OPTIONS, 'install', '-y', *plan['requests']]
            options = {'project': str(fixture.root), 'env_id': fixture.env_id, 'timeout': 600,
                       'env': guard.native_env('fedora44'), 'interactive': False, 'stream': False,
                       'on_guest': Mock()}
            with patch.object(guard, 'AMD_DNF_SUCCEEDED_QUERY', approved_query), \
                 patch.object(guard, 'verify_hardware_permission'), \
                 patch.object(guard, 'amd_install_submission_time'), \
                 patch.object(guard, 'command_timeout', side_effect=lambda attempt, seconds: seconds), \
                 patch.object(reuse, 'process', return_value=owner), \
                 patch.object(reuse, 'capture_binding'), \
                 patch.object(guard.PodmanRuntime, 'package_exec') as native:
                runtime.package_exec(cfg['cid'], logical, **options)
                native.assert_called_once_with(cfg['cid'], pinned, **{
                    **options, 'timeout': guard.AMD_DNF_INSTALL_NATIVE_TIMEOUT,
                    'env': {**options['env'], 'DNF5_PLUGINS_DIR': ''}})
                with self.assertRaises(FileExistsError):
                    runtime.package_exec(cfg['cid'], logical, **options)
                attempt.spec['guest_profile'] = guard.AMD_DNF_PROFILE
                with self.assertRaisesRegex(RuntimeError, 'fixed offline'):
                    runtime.package_exec(cfg['cid'], logical, **options)

    def test_prefetch_transfer_checks_raw_tls_conditions_and_audit_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            audit = source/'transfers/updates'
            audit.mkdir(mode=0o700, parents=True)
            base = 'python3-libs-3.14.10-1.fc44.x86_64.rpm'
            url = guard.AMD_DNF_MIRRORS['updates']+'Packages/p/'+base
            row = {'location': 'Packages/p/'+base, 'repository': 'updates',
                   'download_bytes': 100, 'url': url}
            attempt = {'completed_unix': 10, 'url': url, 'returncode': 0,
                       'payload_bytes': 100, 'error': None}
            receipt = {'url': url, 'url_effective': url, 'size_download': 100, 'size_header': 40,
                       'http_code': 200, 'num_redirects': 0, 'ssl_verify_result': 0, 'exitcode': 0}
            attempt_raw = guard.json.dumps(attempt).encode()
            curl_raw = (guard.json.dumps(receipt)+'\n100\nidentity\n').encode()
            (audit/(base+'.attempt.json')).write_bytes(attempt_raw)
            curl = audit/(base+'.curl')
            curl.write_bytes(curl_raw)
            transfer = {'source': 'continuation', 'completed_unix': 10, 'size_download': 100,
                        'size_header': 40, 'attempt_sha256': hashlib.sha256(attempt_raw).hexdigest(),
                        'curl_sha256': hashlib.sha256(curl_raw).hexdigest()}
            guard.verify_amd_prefetch_transfer(source, row, transfer)
            rejected = (guard.json.dumps({**receipt, 'ssl_verify_result': 1})+'\n100\nidentity\n').encode()
            curl.write_bytes(rejected)
            with self.assertRaisesRegex(RuntimeError, 'transfer evidence changed'):
                guard.verify_amd_prefetch_transfer(source, row, transfer)
            with self.assertRaisesRegex(RuntimeError, 'transport receipt differs'):
                guard.verify_amd_prefetch_transfer(
                    source, row, {**transfer, 'curl_sha256': hashlib.sha256(rejected).hexdigest()})
