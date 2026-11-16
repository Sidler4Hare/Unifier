"""Host and container contracts for the two approved v008 DRM fixtures."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from unifier.cuda_device import CUDA_LABEL, CUDA_LIBRARY, CudaDevice, require_cuda_identity

MIB = 1024**2
AMD_DNF_SAVED_MEMORY = 384*MIB
AMD_DNF_ACTIVE_MEMORY = 768*MIB
AMD_DNF_ACTIVE_HIGH = 640*MIB
NODE = '/dev/dri/renderD128'
NODE_MAPPING = {'source': NODE, 'destination': NODE, 'type': 'bind',
                'options': ['slave', 'nosuid', 'noexec', 'rw', 'rbind']}
CID = re.compile(r'[0-9a-f]{64}', re.ASCII)
CONTAINER_ACTIONS = {'create', 'init', 'start', 'restart', 'exec', 'exec_create',
                     'exec_start', 'unpause', 'restore', 'remove', 'update'}
RFC3339_EVENT = re.compile(
    r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})'
    r'(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})', re.ASCII)


def amd_event_time_ns(event):
    """Podman 4.x exposes an offset-bearing RFC3339Nano Time in its event JSON."""
    if 'timeNano' in event:
        return event['timeNano'] if type(event['timeNano']) is int else None
    value = event.get('Time')
    match = RFC3339_EVENT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    seconds, fraction, offset = match.groups()
    try:
        instant = datetime.fromisoformat(seconds + ('+00:00' if offset == 'Z' else offset))
    except ValueError:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = instant - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int((fraction or '').ljust(9, '0'))


def validate_amd_bootstrap_history(fixture, cid, old_events, history, *,
                                   create_intent, start_intent, aborted, stopped, command_end):
    """Only the recorded bootstrap start and its stops may exist for this CID."""
    if [event.get('Status') for event in old_events] != ['create', 'init', 'start', 'died']:
        raise RuntimeError('AMD bootstrap worker events differ from the interrupted creation')
    if [event for event in history if event.get('Status') not in {'stop', 'cleanup'}] != old_events:
        raise RuntimeError('AMD bootstrap native history differs or contains later activity')
    timestamps = [amd_event_time_ns(event) for event in history]
    if (any(value is None for value in timestamps) or timestamps != sorted(timestamps)
            or len(history) > len(old_events)+2
            or any(sum(event.get('Status') == terminal for event in history) > 1
                   for terminal in ('stop', 'cleanup'))):
        raise RuntimeError('AMD bootstrap native history is ambiguous')
    for event, stamp in zip(history, timestamps):
        if (event.get('ID') != cid or event.get('Type') != 'container'
                or event.get('Name') != fixture.name or event.get('Image') != fixture.reference
                or not matches_labels(fixture, event.get('Attributes'))
                or event.get('Status') in {'stop', 'cleanup'} and not aborted <= stamp <= command_end + 2_000_000_000):
            raise RuntimeError('AMD bootstrap has an unbound native event')
    create, init, start, died = map(amd_event_time_ns, old_events)
    started = amd_event_time_ns({'Time': stopped['StartedAt']})
    finished = amd_event_time_ns({'Time': stopped['FinishedAt']})
    if (started is None or finished is None or old_events[-1].get('ContainerExitCode') != 143
            or not create_intent <= create <= start_intent <= aborted < init <= started <= start <= finished <= died <= command_end
            or not 0 < finished-started < 1_000_000_000):
        raise RuntimeError('AMD bootstrap did not stop the one previously bound init')


def amd_dnf_effective_limits(before, scope):
    """Project the one authorized hard-limit change onto the verified cgroup chain."""
    if (before.get('memory.max') != AMD_DNF_SAVED_MEMORY
            or before.get('memory.swap.max') != 128*MIB or before.get('pids.max') != 128
            or before.get('cpu.max') != {'quota': 100000, 'period': 100000}):
        raise RuntimeError('AMD DNF guest pre-update hard limits differ')
    if sum(entry['cgroup'] == scope for entry in before['chain']) != 1:
        raise RuntimeError('original AMD DNF memory limit is not unique to the verified scope')
    chain = []
    for entry in before['chain']:
        limits = entry['limits']
        if entry['cgroup'] == scope:
            if limits['memory.max'] != str(AMD_DNF_SAVED_MEMORY):
                raise RuntimeError('AMD DNF scope does not own the original memory limit')
            limits = {**limits, 'memory.max': str(AMD_DNF_ACTIVE_MEMORY)}
        elif limits['memory.max'] != 'max' and int(limits['memory.max']) < AMD_DNF_ACTIVE_MEMORY:
            raise RuntimeError('another cgroup limits the approved AMD DNF scope')
        chain.append({**entry, 'limits': limits})
    return {**before, 'memory.max': AMD_DNF_ACTIVE_MEMORY, 'chain': chain}


@dataclass(frozen=True)
class Fixture:
    owner: str
    uid: int
    env_id: str
    root: Path
    name: str
    slice: str
    registry: str
    manifest: str
    image_id: str
    image_size: int
    pci: str
    vendor: str
    device: str
    driver: str
    node_mode: int
    node_gid: int
    cuda: CudaDevice | None = None

    @property
    def network(self):
        return 'none' if self.cuda else 'pasta'

    @property
    def devices(self):
        return () if self.cuda else (NODE,)

    @property
    def shared(self):
        return self.root.parent / 'shared'

    @property
    def labels(self):
        return {'multilinux.project': str(self.root), 'multilinux.env': self.env_id,
                'multilinux.role': 'environment'}

    @property
    def digest(self):
        return 'sha256:' + self.manifest

    @property
    def reference(self):
        return self.registry + '@' + self.digest

    @property
    def pci_path(self):
        return Path('/sys/bus/pci/devices') / self.pci


FIXTURES = {
    'intel': Fixture(
        'omg', 1000, 'debian13',
        Path('/home/omg/文档/多兼容linux机/.lab/hardware-v008-drm-20261115a/environment'),
        'ml-a158811c-debian13', 'unifier-tests.slice', 'docker.io/library/debian',
        '918311b7b6c4c6f68b232ba516584925f6c78ad82b6fd534b98979df6438e483',
        '567e3c17b188e21cd42aa5e1c55e16d8270f880ed8ea321dbd40f993467efc4c',
        162520266, '0000:00:02.0', '8086', '9a49', 'i915', 0o666, 105),
    'amd': Fixture(
        'agent', 1001, 'fedora44',
        Path('/home/agent/unifier-hardware-v008-drm-20261115a/environment'),
        'ml-93dd6c15-fedora44', 'unifier-bridge-tests.slice',
        'registry.fedoraproject.org/fedora',
        '10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011',
        '4b5d9a75c81b96b542ac08b19587efa516da72bf02581908c6178d39de4bb07b',
        382082984, '0000:01:00.0', '1002', '6778', 'radeon', 0o660, 993),
}

V100_INPUT_ROOT = Path('/home/agent/v008-v100-20261116-214dwxyS')
V100_PROBE = '/run/unifier-cuda/v100_known_answers'
V100_CUBIN = '/run/unifier-cuda/v100_known_answers.sm70.cubin'
V100_LIBRARY = '/usr/lib/x86_64-linux-gnu/libcuda.so.580.178.04'
V100_INPUTS = {
    CUDA_LIBRARY: (Path(V100_LIBRARY), 'dcdf334aeb177c95b3385b797c9b871d03a287895713c6d107f79096b0985aa9'),
    V100_PROBE: (V100_INPUT_ROOT/'v100_known_answers', 'a643265fcce26927003cf8d1aed741287c36893943c42c9ea2c763889ef1b67b'),
    V100_CUBIN: (V100_INPUT_ROOT/'v100_known_answers.sm70.cubin', '51958153b754b6fdda8709159ab992d43872054020e070cc7ce829c1a60d039a'),
}
for slot, token, pci, gpu_uuid, minor in (
    ('a', 'ee86a521', '0000:03:00.0', 'GPU-711a11e2-5b33-9a0c-2c9d-4dbfa0ebbca9', 0),
    ('b', '62005bd7', '0000:04:00.0', 'GPU-fca67906-4c5b-f1ed-8396-745631927e19', 1),
):
    amd = FIXTURES['amd']
    FIXTURES['v100-'+slot] = Fixture(
        amd.owner, amd.uid, amd.env_id,
        Path(f'/home/agent/unifier-hardware-v008-v100-214dwxyS-{slot}/environment'),
        f'ml-{token}-fedora44', amd.slice, amd.registry, amd.manifest, amd.image_id,
        amd.image_size, pci, '10de', '1db1', 'nvidia', 0o666, 0,
        CudaDevice(gpu_uuid, pci, minor, V100_LIBRARY))


def image_id(value):
    return value.removeprefix('sha256:') if isinstance(value, str) else None


def validate_image(fixture, value, *, by_config_id=False):
    digest = value.get('Digest')
    repo_digests = value.get('RepoDigests')
    if (image_id(value.get('Id')) != fixture.image_id
            or value.get('Size') != fixture.image_size
            or not isinstance(repo_digests, list) or fixture.reference not in repo_digests
            or value.get('Os') != 'linux' or value.get('Architecture') != 'amd64'):
        raise RuntimeError('cached image fingerprint differs from the approved fixture')
    if by_config_id:
        if (not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
                or fixture.registry+'@'+digest not in repo_digests):
            raise RuntimeError('config-ID image digest is not bound to the approved repository')
    elif digest != fixture.digest:
        raise RuntimeError('locked image lookup returned an unexpected manifest digest')
    return digest


def matches_labels(fixture, labels):
    return isinstance(labels, dict) and {
        k: v for k, v in labels.items() if k.startswith('multilinux.')
    } == fixture.labels


def validate_container(fixture, value, *, cid, image_digest):
    hc = value['HostConfig']
    cfg = value['Config']
    if (not CID.fullmatch(cid) or value.get('Id') != cid
            or value.get('Name') != fixture.name
            or not matches_labels(fixture, cfg.get('Labels'))
            or value.get('ImageName') != fixture.reference
            or image_id(value.get('Image')) != fixture.image_id
            or not isinstance(image_digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_digest)
            or value.get('ImageDigest') != image_digest):
        raise RuntimeError('container identity or image differs from the approved fixture')
    # Rootless Podman represents --device as an OCI bind, so its Docker-style
    # inspect Devices array does not show this node. Validate that bind below.
    if (hc.get('Devices') or hc.get('DeviceRequests')
            or hc.get('DeviceCgroupRules') or hc.get('CDIDevices')
            or hc.get('NetworkMode') != fixture.network or hc.get('SecurityOpt')
            or hc.get('UsernsMode') not in (None, '', 'private', 'host')
            or hc.get('UTSMode') not in (None, '', 'private')
            or hc.get('CapAdd') or hc.get('GroupAdd')
            or hc.get('VolumesFrom') or hc.get('PublishAllPorts') or hc.get('PortBindings')
            or cfg.get('User') not in ('', '0', '0:0')
            or any(item.split('=', 1)[0].lower().endswith('_proxy')
                   for item in (cfg.get('Env') or []))):
        raise RuntimeError('container device, network, user or security configuration differs')
    mounts = value['Mounts']
    if fixture.cuda:
        expected = {(str(source), destination) for destination, (source, _) in V100_INPUTS.items()}
        if (len(mounts) != len(expected)
                or {(m.get('Source'), m.get('Destination')) for m in mounts} != expected
                or any(m.get('Type') != 'bind' or m.get('RW') is not False for m in mounts)
                or json.loads((cfg.get('Labels') or {}).get(CUDA_LABEL, 'null')) != fixture.cuda.identity()):
            raise RuntimeError('saved CUDA inputs or attachment identity changed')
        return
    if len(mounts) != 1 or mounts[0].get('Type') != 'bind' or mounts[0].get('RW') is not True:
        raise RuntimeError('container does not have one writable fixture bind mount')


def validate_oci(fixture, spec):
    if spec.get('hooks') or spec['linux'].get('devices'):
        raise RuntimeError('unapproved OCI hooks or device nodes')
    mounts = spec['mounts']
    if fixture.cuda:
        validate_cuda_oci(fixture, mounts)
        return
    device_binds = [mount for mount in mounts if mount.get('type') == 'bind'
                    and any(str(mount.get(key, '')).startswith('/dev/')
                            for key in ('source', 'destination'))]
    expected = NODE_MAPPING
    if (len(device_binds) != 1
            or any(device_binds[0].get(key) != expected[key] for key in ('source', 'destination', 'type'))
            or set(device_binds[0].get('options', [])) != set(expected['options'])):
        raise RuntimeError('saved OCI device bind differs from the approved single writable render node')
    allowed = {(str(fixture.shared), str(fixture.shared)), (NODE, NODE),
               ('/sys', '/sys'), ('/sys/fs/cgroup', '/sys/fs/cgroup')}
    init_sources = {'/usr/libexec/podman/catatonit', '/usr/libexec/catatonit/catatonit',
                    '/usr/bin/catatonit'}
    for mount in mounts:
        pair = (mount.get('source'), mount.get('destination'))
        if mount.get('type') == 'bind' and pair not in allowed:
            if (pair[0] in init_sources and pair[1] == '/run/podman-init'
                    and 'ro' in mount.get('options', [])):
                continue
            raise RuntimeError('unapproved OCI host bind mount: '+str(pair))


def validate_cuda_oci(fixture, mounts):
    devices = set(fixture.cuda.nodes)
    expected = {destination: str(source) for destination, (source, _) in V100_INPUTS.items()}
    inputs_seen, devices_seen = set(), set()
    init_seen = False
    for mount in mounts:
        source, dest = mount.get('source'), mount.get('destination')
        kind = mount.get('type')
        options = set(mount.get('options') or ())
        if kind != 'bind':
            if kind not in {'proc', 'tmpfs', 'devpts', 'mqueue', 'cgroup', 'cgroup2', 'sysfs'}:
                raise RuntimeError('unexpected CUDA namespace mount type')
        elif dest in devices and source == dest and dest not in devices_seen:
            if options != set(NODE_MAPPING['options']):
                raise RuntimeError('unexpected CUDA device bind options')
            devices_seen.add(dest)
        elif dest in expected and source == expected[dest] and dest not in inputs_seen:
            if not {'ro', 'nosuid', 'nodev'} <= options or {'rw', 'shared', 'rshared'} & options:
                raise RuntimeError('unexpected CUDA read-only input mount options')
            inputs_seen.add(dest)
        elif (source in {'/usr/libexec/podman/catatonit', '/usr/libexec/catatonit/catatonit',
                         '/usr/bin/catatonit'} and dest == '/run/podman-init' and 'ro' in options
              and not init_seen):
            init_seen = True
        else:
            raise RuntimeError(f'unexpected CUDA host bind: {source} to {dest}')
    if devices_seen != devices or inputs_seen != expected.keys() or not init_seen:
        raise RuntimeError('incomplete CUDA device or input mount set')


def validate_record(fixture, cid, record):
    env_uuid = record.get('environment_uuid')
    try:
        parsed = UUID(env_uuid)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError('invalid official environment UUID') from exc
    environment = record.get('runtime', {})
    image = record.get('base_image', {})
    if (str(parsed) != env_uuid or parsed.version != 4
            or type(record.get('generation')) is not int or record['generation'] != 1
            or record.get('created_by_uid') != fixture.uid
            or record.get('target') != fixture.env_id or record.get('id') != fixture.env_id
            or record.get('desktop') is not None or image.get('locked_reference') != fixture.reference
            or image.get('manifest_digest') != fixture.digest
            or record.get('shared_mounts') != ([] if fixture.cuda else [
                {'host': str(fixture.shared), 'container': str(fixture.shared), 'options': ''}])
            or environment != {
                'backend': 'podman', 'container_name': fixture.name, 'container_id': cid,
                'project': str(fixture.root), 'network': fixture.network, 'share_label_policy': 'preserve-host',
                'selinux_label': 'default', 'full_init': False, 'user_namespace': 'rootless-default',
                'devices': list(fixture.devices), 'ipc': 'private', 'rootless': True,
                **({'cuda': fixture.cuda.identity()} if fixture.cuda else {}),
            }):
        raise RuntimeError('official environment record differs from the approved fixture')
    return {'cid': cid, 'environment_uuid': env_uuid, 'generation': record['generation']}


def observe_device(fixture):
    if fixture.cuda:
        cuda = require_cuda_identity(fixture.cuda.identity())
        return {name: (Path(name).stat().st_rdev) for name in cuda.nodes}
    node = Path(NODE).lstat()
    device = (Path('/sys/dev/char/226:128') / 'device').resolve(strict=True)
    pci = fixture.pci_path.resolve(strict=True)
    if (not stat.S_ISCHR(node.st_mode) or node.st_uid != 0 or node.st_gid != fixture.node_gid
            or stat.S_IMODE(node.st_mode) != fixture.node_mode
            or (os.major(node.st_rdev), os.minor(node.st_rdev)) != (226, 128)
            or device != pci or (pci / 'vendor').read_text().strip() != '0x' + fixture.vendor
            or (pci / 'device').read_text().strip() != '0x' + fixture.device
            or (pci / 'driver').resolve(strict=True).name != fixture.driver
            or not os.access(NODE, os.R_OK | os.W_OK, effective_ids=True)):
        raise RuntimeError('renderD128 identity or read/write permission differs')
    return {'path': NODE, 'rdev': node.st_rdev, 'dev': node.st_dev, 'inode': node.st_ino,
            'pci': fixture.pci, 'vendor': fixture.vendor, 'device': fixture.device,
            'driver': fixture.driver, 'mode': stat.S_IMODE(node.st_mode)}


def _temperature(sensors, ceiling):
    if not sensors or len(sensors) > 32:
        raise RuntimeError('required temperature measurements are unavailable')
    rows = {}
    for path in sensors:
        value = int(path.read_text().strip())
        critical = path.with_name(path.name.replace('_input', '_crit'))
        limit = min(ceiling, int(critical.read_text().strip())) if critical.exists() else ceiling
        if value < -30000 or value >= limit or limit <= 0:
            raise RuntimeError('temperature limit reached or sensor unreadable: ' + str(path))
        rows[str(path)] = value
    return rows


def verify_cuda_inputs(fixture):
    for source, digest in V100_INPUTS.values():
        info = source.lstat()
        owner = 0 if str(source) == fixture.cuda.library else fixture.uid
        if (source.resolve(strict=True) != source or not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner or info.st_mode & 0o022 or not 0 < info.st_size < 128*MIB):
            raise RuntimeError(f'CUDA input is not a trusted bounded file: {source}')
        with source.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise RuntimeError(f'CUDA input digest changed: {source}')


def observe_gpu(fixture, payload_bytes):
    """Only the explicit render phase requires real telemetry, before any submission."""
    cpu_sensors = [
        path for sensor in Path('/sys/class/hwmon').glob('hwmon*')
        if (sensor / 'name').read_text().strip() in {'coretemp', 'k10temp', 'zenpower'}
        for path in sensor.glob('temp*_input')
    ]
    cpu = _temperature(cpu_sensors, 80000)
    if fixture.driver == 'i915':
        package = [path for path in cpu_sensors
                   if path.with_name(path.name.replace('_input', '_label')).exists()
                   and path.with_name(path.name.replace('_input', '_label')).read_text().strip()
                   == 'Package id 0']
        if len(package) != 1:
            raise RuntimeError('Intel package temperature proxy is unavailable')
        return {'cpu_millicelsius': cpu, 'package_proxy_millicelsius': _temperature(package, 70000),
                'gpu_temperature_source': 'coretemp-package-proxy',
                'gpu_memory_source': 'shared-host-memory', 'payload_bytes': payload_bytes}
    gpu = _temperature(list(fixture.pci_path.glob('hwmon/hwmon*/temp*_input')), 70000)
    from fixtures.hardware.drm_egl_probe import radeon_memory
    observed = observe_device(fixture)
    fd = os.open(NODE, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        node = os.fstat(fd)
        if (node.st_dev, node.st_ino, node.st_rdev) != (observed['dev'], observed['inode'], observed['rdev']):
            raise RuntimeError('Radeon DRM descriptor no longer matches the verified node')
        memory = radeon_memory(fd, {})
    finally:
        os.close(fd)
    if memory['free_bytes']-payload_bytes < memory['reserve_bytes']:
        raise RuntimeError('GPU memory reserve is unavailable')
    return {'gpu_millicelsius': gpu, 'cpu_millicelsius': cpu, 'vram': memory,
            'payload_bytes': payload_bytes}
