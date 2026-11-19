"""Run the approved v008 hardware fixtures under the independent test supervisor."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import runpy
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]

from tools import reuse_test_guard as reuse
from tools import vulkan_probe
from tools import v100_media_probe
from tools import intel_media_guard
from tools import arch_gpu_packages
from tools.hardware_guard_policy import (
    AMD_DNF_ACTIVE_HIGH, AMD_DNF_ACTIVE_MEMORY, AMD_DNF_SAVED_MEMORY, CID, CONTAINER_ACTIONS,
    FIXTURES, MIB, NODE, amd_dnf_effective_limits, amd_event_time_ns, matches_labels, observe_device,
    observe_gpu, validate_amd_bootstrap_history, validate_container, validate_image, validate_oci, validate_record,
    V100_CUBIN, V100_INPUTS, V100_INPUT_ROOT, V100_PROBE, verify_cuda_inputs,
)
from unifier import package_journal
from unifier.identity import binding_for
from unifier.project import load_project
from unifier.exec_control import SCRIPT_PATH
from unifier.runtime import CreateSpec, PodmanRuntime, SLEEPER, _run_podman, container_state_from_inspect, creation_argv
from unifier.packages import install_commands, inventory_argv, native_env, validate_package_name
from unifier.store import EnvironmentStore

ENTRY = Path(__file__).resolve()
PYTHON = '/usr/bin/python3'
BOOTSTRAP_PHASES = {'create', 'resume', 'amd-bootstrap-recover'}
BUILTIN_PHASES = BOOTSTRAP_PHASES | {'recover', 'vulkan', 'media-recover'}
PACKAGE_MAINTENANCE_PHASES = {'native-repair', 'recover'}
PHASES = BUILTIN_PHASES | {'prepare', 'gpu', 'native-repair', 'media'}
LOCK = ROOT / 'config/images.lock.json'
QQ_STANDARD_CONTROLLER = {'memory_max': 512*MIB, 'memory_high': 448*MIB, 'tasks_max': 128}
ARCH_EVIDENCE = ROOT/'artifacts/v008-arch-gpu-20261117-HIiJexUu'
ARCH_RECOVERY_TXN = 'txna50c6285ae92'
ARCH_RECOVERY_CID = 'd62e7139aaaae731299992fd19f722dd21c4bec5b1e39245f73542fd84a3088f'
ARCH_TRANSACTION_SHA = '536d9403ddc5e8e94f2b8a0183aae90c618e825e183a80c89523e3a7b2736b6d'
ARCH_MAINTENANCE_PHASES = {'replay', 'replay-llvm', 'reconcile'}
INTEL_RESUME_CID = '9a6c635b14d4e3ea63b6af2f767420ef55a5191011c29c203525f3cd32088b88'
INTEL_RESUME_FROM = (FIXTURES['intel'].root.parents[2]
                     /'artifacts/v008-gpu-drm-20261115T183930+0800/intel-create')
INTEL_RETRY_FROM = INTEL_RESUME_FROM.with_name('intel-resume')
INTEL_VALIDATOR_RETRY_FROM = INTEL_RESUME_FROM.with_name('intel-resume-utf8')
INTEL_RETRY_START_ERROR = 'conmon failed: exit status 1'
AMD_BOOTSTRAP_FROM = Path('/home/agent/v008-gpu-drm-20261115T183930/amd-create-m96')
AMD_BOOTSTRAP_CID = '2180a83540510175c2d177c0decf0cf852788fbe77d6c6263d98737fd644fa3f'
AMD_BOOTSTRAP_MARKER = 'amd-bootstrap-recovery-attempt.json'
V100_A_SETUP_ATTEMPT = V100_INPUT_ROOT/'live-wNFwRB1g/a-create'
V100_TENSOR_FILES = ('v100_known_answers', 'v100_known_answers.sm70.cubin')
AMD_DNF_PROFILE = 'amd-dnf-retry-768'
AMD_DNF_INSTALL_PROFILE = 'amd-dnf-offline-install-768'
AMD_DNF_INSTALL_RETRY_PROFILE = 'amd-dnf-offline-install-retry-768'
AMD_DNF_INSTALL_REFRESH_PROFILE = 'amd-dnf-offline-install-refresh-768'
AMD_DNF_INSTALL_PROFILES = {AMD_DNF_INSTALL_PROFILE, AMD_DNF_INSTALL_RETRY_PROFILE, AMD_DNF_INSTALL_REFRESH_PROFILE}
AMD_DNF_MEMORY_PROFILES = {AMD_DNF_PROFILE, *AMD_DNF_INSTALL_PROFILES}
AMD_DNF_INSTALL_FROM = AMD_BOOTSTRAP_FROM.with_name('amd-package-install')
AMD_DNF_INSTALL_RETRY_FROM = AMD_BOOTSTRAP_FROM.with_name('amd-package-install-retry')
AMD_DNF_REFRESH_FROM = AMD_BOOTSTRAP_FROM.with_name('fedora-metadata-refresh')
AMD_DNF_REFRESH_METALINK = AMD_DNF_REFRESH_FROM/'metalink/metalink-receipt.json'
AMD_DNF_INSTALL_RETRY_FAILURE_HASHES = {
    'result.json': '746fa20487cf747c19e511f7b9714328b63ca3c1c07238fa3991bbb79f214768',
    'receipt.json': '87e24a8f95cde8782c99beeb28f65f1a6eef4303d31a57974057c670c0ff5c52',
    'command-result.json': 'e81ab481572e73a7f24be6e6aad9ab81bce06a193f8004e490e42602c03bf816',
    'cleanup.json': 'd8dd888814065521e1d62ee86462fa1aa8e14b2e5802e92d23a40faf50eb40d3',
    'rescue.json': 'd8dd888814065521e1d62ee86462fa1aa8e14b2e5802e92d23a40faf50eb40d3',
    'native-after.json': '454ec14e7d160cabc4a54cb722f96c5660aea03b557d7814fc7a0dc74b921ade',
    'non-target-before.json': '886f2b384733b36ba37137b6a04ee2ffa2f11533eeea4f20e265227158c68389',
    'spec.json': '2af4d0ac2725c3461a39b74900160c26ddb22de597144d808487880c030cce17',
    'amd-guest-memory-after.json': '014785b037fffb3862df837073d28975dc74e37d69f51a1ec67fed9fd53fe5ff',
    'amd-offline-install-before.json': 'bad9cee004d34c32648f197850964fcc29a381fe927bdeaf22c48a0df4b2a0c2',
    '01-dnf-main.stdout': '6b9656d567a1f85c425deef006e4d28872103352f82f8a7b12a39283a4f5f867',
    '02-dnf-repos.stdout': '83e9ed3babfa486262d7f5c5a97e547e8837055ac067fab3433d329e129e18b2',
    '05-verify-cached-rpms.stdout': '6ac3ee19d36b9d2065b4f8ec535443003a636d1dbfebf2e23019c5477f8949ed',
    '06-rpm-signatures.stdout': 'ecd4ea391827d2f4bb7ab3743e843bb1c66c83a725af21f784a61efdd4012649',
    '06-rpm-signatures.stderr': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    '08-dnf-solver.stdout': 'c24b19e765509427473ec3a9f82f414cebc71d8134ace6947015139bed13a658',
    '09-rpm-inventory.stdout': '326390ced4ad6fa2379ee2046f89acc1ca96055c338d845d5a106a92e4767238',
    '10-dnf-package-sizes.stdout': 'd5ae97f405074907f5eb540beaed0f9d1393d2c2228b95e32fb9ebeb78f348fb',
}
AMD_DNF_INSTALL_TRANSACTION = 'txn124e64406e4c'
AMD_DNF_INSTALL_UNCHANGED_INVENTORY = '4fbc88c890ab44ee974f34584be338afd98956bdb78224fc6c8c05b0bb722801'
AMD_DNF_INSTALL_FAILURE_HASHES = {
    'result.json': '2c1e4ce4599f836160fb0b06e6559df64723050d9f992dde6929e8dc652ab592',
    'spec.json': '41ef7ab302ec7d1bc91c4e54a95ffdbb7c9f1b0fced81161285e785b42488d9c',
    'package-transaction.json': 'd67712d5904bec2c3242b0ceb474023be3e4392fa27c488169036a070768cfdb',
    'receipt.json': '189265b855f4852cdf9c409e5539fd74f04240a6a1d7e7c34d20b6b8b09ec772',
    'cleanup.json': '887911f841eab6f86e02d8e15db9a4857437deb1bf2d4993b4caa7e242ed6ab7',
    'command-result.json': 'ea9080dbb82b9b8cbca955d4d3d1f0d9973ba96ecd4583bcd137dda971a5247f',
    'amd-offline-install-ready.json': '358496e2ba21779f9719484f2488fbdc1c7fc956fc0f86d4bf8e87fc405a6b6d',
    'amd-offline-install-before.json': 'bad9cee004d34c32648f197850964fcc29a381fe927bdeaf22c48a0df4b2a0c2',
    '06-rpm-signatures.stdout': 'ecd4ea391827d2f4bb7ab3743e843bb1c66c83a725af21f784a61efdd4012649',
    '06-rpm-signatures.stderr': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    '01-dnf-main.stdout': '6b9656d567a1f85c425deef006e4d28872103352f82f8a7b12a39283a4f5f867',
    '02-dnf-repos.stdout': '4710779104be1403809c83ba4a6ebd4822115c4e22484cfd46460429a923fb42',
    'native-after.json': '886f2b384733b36ba37137b6a04ee2ffa2f11533eeea4f20e265227158c68389',
    'rescue.json': '887911f841eab6f86e02d8e15db9a4857437deb1bf2d4993b4caa7e242ed6ab7',
    'amd-guest-memory-after.json': '40de0db77aa47e9a09fd01bc1f4ede737684a3cbda513aa68c6e8f38aade5786',
    'admitted-package-step-0.json': 'd848c631de999a1b84bcab1290095dadae6842f1d6860f166bd924d819fb7de1',
    '05-import-authenticated-rpms.stdout': '6ac3ee19d36b9d2065b4f8ec535443003a636d1dbfebf2e23019c5477f8949ed',
    '08-dnf-solver.stdout': 'c24b19e765509427473ec3a9f82f414cebc71d8134ace6947015139bed13a658',
    '10-dnf-package-sizes.stdout': 'd5ae97f405074907f5eb540beaed0f9d1393d2c2228b95e32fb9ebeb78f348fb',
}
AMD_DNF_ABORT_FROM = AMD_BOOTSTRAP_FROM.with_name('amd-package-query')
AMD_DNF_SUCCEEDED_QUERY = AMD_BOOTSTRAP_FROM.with_name('amd-query-memory')
AMD_DNF_INSTALL_INPUTS = AMD_BOOTSTRAP_FROM.with_name('fedora-install-inputs')
AMD_DNF_INSTALL_COMMAND = [PYTHON, '-B', str(AMD_DNF_INSTALL_INPUTS/'fedora-install-controller.py')]
AMD_DNF_INSTALL_CONTROLLER_SHA256 = '3c6a8ed16e312d41d7768d438ab38be6eb5b5644468ead2171f3f5aff2c7b1f5'
AMD_DNF_INSTALL_RETRY_INPUTS = AMD_BOOTSTRAP_FROM.with_name('fedora-install-retry-inputs')
AMD_DNF_INSTALL_RETRY_COMMAND = [PYTHON, '-B', str(AMD_DNF_INSTALL_RETRY_INPUTS/'fedora-install-controller.py')]
AMD_DNF_INSTALL_RETRY_CONTROLLER_SHA256 = '78219f33b2f1fd6f413cfdcc13072b1f711e4de1cfae9f28e7b1fd97f690e3a1'
AMD_DNF_INSTALL_REFRESH_INPUTS = AMD_BOOTSTRAP_FROM.with_name('fedora-install-refresh-inputs')
AMD_DNF_INSTALL_REFRESH_COMMAND = [PYTHON, '-B', str(AMD_DNF_INSTALL_REFRESH_INPUTS/'fedora-install-controller.py')]
AMD_DNF_INSTALL_REFRESH_CONTROLLER_SHA256 = 'ce8ed4e485dc0bf1c08887e5699043fcb905b35c229c59526dedeede1d7b4dfe'
AMD_DNF_PRIMARY = AMD_BOOTSTRAP_FROM.with_name('fedora-primary-inputs')/'primary-checksums.json'
AMD_DNF_PRIMARY_SHA256 = '5f7627e03a2ccf2d47c938ade42ada547e5101a4a3701449f384d8d29f4cfdbf'
AMD_DNF_QUERY_SHA256 = '038b6de3dec18502bf3c3fcb649fc54af5d395a94fa6dffd88102e8db6cc2128'
AMD_DNF_PREFETCH_ORIGINAL = AMD_BOOTSTRAP_FROM.with_name('fedora-package-prefetch')
AMD_DNF_PREFETCH = AMD_BOOTSTRAP_FROM.with_name('fedora-package-prefetch-continuation')/'result.json'
AMD_DNF_METALINK = AMD_DNF_PREFETCH_ORIGINAL/'metalink/metalink-receipt.json'
AMD_DNF_PREFETCH_PREVIOUS_HASHES = {
    'result.json': '81636e4c6943ae7a986986f49d81d74b4dfa75c2f783e7b000153de8a437f051',
    'receipt.json': 'f864045c4a2cc76474d1ce62d47b393a539f1ce855f746437dbdbb653de3c46d',
    'cleanup.json': '64a0bbd622d9b703b563a0f5e5069bee2cd0763f6d0589397df3f996a1a38117',
    'command-result.json': 'c4a627eb84402602f7a3591c8f34d6dd942f2bdc7540d9c9ca61c6b8adf97b29',
    'metalink/metalink-receipt.json': '398f8bba7744b7a4cbf56d0160508183b9c259fc306f662bcaf9c7dd7bea30ff',
}
AMD_DNF_PREFETCH_TIMEOUT = 'one-shot RPM fetch failed: python3-libs-3.14.10-1.fc44.x86_64.rpm (curl 28)'
AMD_DNF_PREFETCH_REMAINING = {'python3-libs', 'spirv-tools-libs'}
AMD_DNF_PREFETCH_EXPIRY = 1794751539
AMD_DNF_PREFETCH_PARTIAL_ID = (64512, 13895499, 33152, 1001, 8420948, 1794749873872066096)
AMD_DNF_PREFETCH_TIMEOUT_TRANSFER = {
    'source': 'original-prefetch', 'completed_unix': 1794749873, 'size_header': 428,
    'size_download': 8420948,
    'attempt_sha256': '3dd72d01a43e2b86cc1655d0c0e6ba747dbf9dce22ed919eb20ffd41d66b74f8',
    'curl_sha256': '73167678fa1eaf6dbe498df026813be9a4b923016b9c89a5508a2b7ba1099c07',
}
AMD_DNF_PAYLOAD = FIXTURES['amd'].shared/'fedora-rpms-20261115a'
AMD_DNF_MIRRORS = {
    'fedora': 'https://ftp.fau.de/fedora/linux/releases/44/Everything/x86_64/os/',
    'updates': 'https://mirror.23m.com/fedora/linux/updates/44/Everything/x86_64/',
}
AMD_DNF_CACHE_ROOT = '/var/cache/unifier-fedora-query-continuation-20261115a'
AMD_DNF_SIGNING_FINGERPRINT = '36f612dcf27f7d1a48a835e4dbfcf71c6d9f90a6'
AMD_DNF_OFFLINE_OPTIONS = [
    '--cacheonly', '--no-plugins', '--setopt=cachedir='+AMD_DNF_CACHE_ROOT,
    '--setopt=system_cachedir='+AMD_DNF_CACHE_ROOT, '--setopt=gpgcheck=1',
]
AMD_DNF_EXACT_PLAN = {
    'requests': ['python3-0:3.14.10-1.fc44.x86_64', 'mesa-libgbm-0:26.2.6-1.fc44.x86_64',
                 'libglvnd-egl-1:1.7.0-9.fc44.x86_64', 'mesa-libEGL-0:26.2.6-1.fc44.x86_64',
                 'libglvnd-gles-1:1.7.0-9.fc44.x86_64', 'mesa-dri-drivers-0:26.2.6-1.fc44.x86_64'],
    'download_bytes': 67971267, 'installed_bytes': 274202076,
    'signed_repository': 'fedora-44: fedora,fedora-cisco-openh264,updates RPM OpenPGP',
}
AMD_DNF_ABORT_COMMAND = [PYTHON, str(AMD_BOOTSTRAP_FROM.parent/'fedora-alternate-inputs/fedora-cache-controller.py'),
                         'query', '--allow-updates-alternate']
AMD_DNF_RETRY_COMMAND = [PYTHON, '-B',
                         str(AMD_BOOTSTRAP_FROM.parent/'fedora-query-continuation-inputs/fedora-cache-controller.py'),
                         'query-continuation']
AMD_DNF_RETRY_CONTROLLER_SHA256 = '22335a8d9e07d2f704b8dee09c39beaefb1d6ffeda1451ba61a47e2323872f52'
AMD_DNF_LIBRARIES = {
    'fedora-cache-stage.py': '0b332dfd353ab3da3c495401a44e6a9f242ad435d6968eff2abad25cd7558717',
    'package-controller.py': 'aaeefc368d336b4d6fd675a3b1bb97cedfbb93ca434b1f6c3626f0220568cc8a',
    'fedora-cache-controller.py': AMD_DNF_RETRY_CONTROLLER_SHA256,
}
AMD_DNF_BINDING = {'environment_id': 'fedora44',
                   'environment_uuid': 'a1e18eb1-72f6-4d99-8178-a4c88cb0e213',
                   'generation': 1, 'container_id': AMD_BOOTSTRAP_CID}
AMD_DNF_RETRY_MARKER = 'amd-dnf-memory-retry-attempt.json'
AMD_DNF_INSTALL_MARKER = 'amd-dnf-offline-install-attempt.json'
AMD_DNF_INSTALL_RETRY_MARKER = 'amd-dnf-offline-install-retry-attempt.json'
AMD_DNF_INSTALL_REFRESH_MARKER = 'amd-dnf-offline-install-refresh-attempt.json'
AMD_DNF_INSTALL_NATIVE_TIMEOUT = 90
AMD_DNF_INSTALL_FINISH_RESERVE = 60
AMD_DNF_LIMIT_INTENT = 'amd-guest-memory-intent.json'
AMD_DNF_LIMIT_APPLIED = 'amd-guest-memory-applied.json'
INTEL_RECOVER_FROM = INTEL_RESUME_FROM.with_name('intel-package-install')
INTEL_REPAIR_FROM = INTEL_RECOVER_FROM.with_name('intel-native-repair')
INTEL_RECONCILIATION = INTEL_REPAIR_FROM.with_name('intel-native-repair-reconciliation.json')
INTEL_REPAIR_CONTROLLER = INTEL_RECOVER_FROM.with_name('intel-native-repair-controller.py')
INTEL_REPAIR_CONTROLLER_SHA256 = '202cc5fc48f2174c9349a7a6894b4093149bcc128375dc0da2284f374ce22f57'
INTEL_REPAIR_PLAN = INTEL_RECOVER_FROM.with_name('intel-package-query')/'result.json'
INTEL_RECOVER_TRANSACTION = 'txne70ac1f03324'
INTEL_RECOVER_BINDING = {'environment_id': 'debian13',
                         'environment_uuid': 'fe835ffd-31cb-4397-94af-57bc5f65941d',
                         'generation': 1, 'container_id': INTEL_RESUME_CID}
INTEL_REPAIR_ARCHIVE = '/var/cache/apt/archives/libllvm19_1%3a19.1.7-3+b1_amd64.deb'
INTEL_OPTIONAL_RECOMMENDATION = {'name': 'libgpm2', 'version': '1.20.7-11+b2', 'arch': 'amd64',
                                'recommended_by': 'libncursesw6'}
INTEL_REPAIR_SIZE = 25978316
INTEL_REPAIR_SHA256 = 'db0d614d61345ca710fb73e75105f1ec6e38898fa7b3aa99fa7eb7d8b0a11d57'
INTEL_REPAIR_REQUESTS = [
    'python3=3.13.5-1', 'libgbm1=25.0.7-2+deb13u1', 'libegl1=1.7.0-1+b2',
    'libegl-mesa0=25.0.7-2+deb13u1', 'libgles2=1.7.0-1+b2', 'libgl1-mesa-dri=25.0.7-2+deb13u1',
]
INTEL_REPAIR_STEPS = {
    'unpack': ['dpkg', '--unpack', INTEL_REPAIR_ARCHIVE],
    'finish-cached-install': ['apt-get', '--no-download', '--no-remove', '--fix-broken',
                              'install', '-y', *INTEL_REPAIR_REQUESTS],
    'audit': ['dpkg', '--audit'],
}
INTEL_REPAIR_READS = (
    ['dpkg-query', '-W', '-f=${Package}\t${Architecture}\t${Version}\t${Status}\n'],
)
ACL_QUEUE_RESERVE = 75
ACL_CLEANUP_RESERVE = 15
ACL_WORKER_CLEANUP = 22


def bound_bytes(path):
    """Read one bounded host-owned file without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise RuntimeError('untrusted host record: ' + str(path))
        data = stream.read(2*MIB+1)
    if len(data) > 2*MIB:
        raise RuntimeError('host record exceeds read budget: ' + str(path))
    return data


def bound_read(path):
    return json.loads(bound_bytes(path))


def owned_directory(path, *, private=False):
    st = path.lstat()
    if (not stat.S_ISDIR(st.st_mode) or path != path.resolve(strict=True)
            or st.st_uid != os.getuid() or st.st_mode & (0o077 if private else 0o022)):
        raise RuntimeError('untrusted fixture directory: ' + str(path))


def absent(path):
    if os.path.lexists(path):
        raise RuntimeError('new fixture path already exists: ' + str(path))


def cpu_pinned(limits):
    try:
        quota, period = map(int, limits['cpu.max'].split())
    except (KeyError, ValueError):
        return False
    return quota == period and period > 0


def clean_environment(env):
    # Pin the local Podman engine and avoid inheriting credentials, remote-engine
    # selection, alternate storage, or preload settings from the desktop.
    keep = {'HOME', 'USER', 'LOGNAME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'}
    values = {key: value for key, value in env.items() if key in keep}
    values.update(PATH='/usr/bin:/bin', GOMAXPROCS='1', LC_ALL='C.UTF-8', LANG='C.UTF-8',
                  PYTHONDONTWRITEBYTECODE='1')
    return values


def package_names(fixture, requests):
    versioned = (r'(?P<name>[a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?=[a-zA-Z0-9][a-zA-Z0-9+:~.\-]*'
                 if fixture == 'intel' else
                 r'(?P<name>[a-zA-Z0-9][a-zA-Z0-9+._\-]*)-[0-9][a-zA-Z0-9+._~:]*'
                 r'-[0-9][a-zA-Z0-9+._~]*\.(?:x86_64|noarch)')
    result = []
    for request in requests:
        if not isinstance(request, str) or len(request) > 256:
            raise ValueError('package request must be a version-locked native argument')
        match = re.fullmatch(versioned, request)
        if match is None:
            raise ValueError('package request must be a version-locked native argument')
        result.append(validate_package_name(match['name']))
    if len(result) != len(set(result)):
        raise ValueError('package plan contains duplicate names')
    return result


def selected_controller_budget(spec):
    extra = ()
    if (spec.get('qq_gui') is True and spec.get('fixture') == 'intel' and spec.get('phase') == 'gpu'
            and spec['unit'].startswith('unifier-drm-qq-gui-standard-')):
        extra = (QQ_STANDARD_CONTROLLER,)
    return reuse.controller_budget(spec, additional=extra)


def validate_spec(spec):
    if isinstance(spec, dict) and spec.get('fixture') == 'arch-intel':
        phase = spec.get('phase')
        fields = {'fixture', 'phase', 'unit', 'seconds'}
        if isinstance(phase, str) and phase in ARCH_MAINTENANCE_PHASES:
            controller = ARCH_EVIDENCE/('arch-'+phase+'.py')
            if (set(spec) != fields | {'command', 'controller_sha256'}
                    or spec['command'] != [PYTHON, str(controller)]
                    or not isinstance(spec['controller_sha256'], str)
                    or not CID.fullmatch(spec['controller_sha256'])
                    or type(spec['seconds']) is not int or not 1 <= spec['seconds'] <= 120
                    or not isinstance(spec['unit'], str)
                    or re.fullmatch(r'unifier-drm-arch-'+phase+r'-[a-z0-9-]{1,24}', spec['unit']) is None):
                raise ValueError('Arch maintenance requires the fixed bounded controller')
            return FIXTURES['arch-intel']
        extras = {'create': set(), 'prepare': {'command'}, 'gpu': {'command', 'gpu_payload_bytes'},
                  'vulkan': {'probe_sha256'}, 'media': {'command'}}
        if phase == 'prepare' and 'arch_plan_sha256' in spec:
            if not isinstance(spec['arch_plan_sha256'], str) or not CID.fullmatch(spec['arch_plan_sha256']):
                raise ValueError('invalid Arch offline package plan digest')
            fields.add('arch_plan_sha256')
        if (not isinstance(phase, str) or phase not in extras
                or set(spec) != fields | extras[phase]
                or type(spec.get('seconds')) is not int or not 1 <= spec['seconds'] <= 120
                or not isinstance(spec.get('unit'), str)
                or re.fullmatch(r'unifier-drm-arch-' + phase + r'-[a-z0-9-]{1,24}', spec['unit']) is None):
            raise ValueError('invalid bounded Arch Intel fixture phase')
        if phase == 'vulkan':
            if not isinstance(spec['probe_sha256'], str) or not CID.fullmatch(spec['probe_sha256']):
                raise ValueError('invalid Arch Intel Vulkan probe digest')
            return FIXTURES['arch-intel']
    if isinstance(spec, dict) and spec.get('phase') == 'media-recover':
        if (set(spec) - {'fixture', 'phase', 'unit', 'seconds', 'previous_stage'}
                or spec.get('previous_stage', 'install') not in ('install', 'install-confirmed-files')
                or spec.get('fixture') != 'intel'
                or type(spec.get('seconds')) is not int or not 1 <= spec['seconds'] <= 120
                or not isinstance(spec.get('unit'), str)
                or re.fullmatch(r'unifier-drm-vaapi-recover-[a-z0-9-]{1,30}', spec['unit']) is None):
            raise ValueError('invalid Intel media recovery phase')
        return FIXTURES['intel']
    if isinstance(spec, dict) and spec.get('phase') == 'vulkan':
        if (set(spec) != {'fixture', 'phase', 'unit', 'seconds', 'probe_sha256'}
                or spec.get('fixture') != 'intel'
                or type(spec.get('seconds')) is not int or not 1 <= spec['seconds'] <= 120
                or not isinstance(spec.get('unit'), str)
                or re.fullmatch(r'unifier-drm-vulkan-[a-z0-9-]{1,30}', spec['unit']) is None
                or not isinstance(spec.get('probe_sha256'), str) or not CID.fullmatch(spec['probe_sha256'])):
            raise ValueError('invalid Intel Vulkan probe specification')
        return FIXTURES['intel']
    if isinstance(spec, dict) and spec.get('fixture') in ('v100-a', 'v100-b', 'arch-v100'):
        allowed = {'fixture', 'phase', 'unit', 'seconds'}
        if ('retry_setup_from' in spec and spec['fixture'] == 'v100-a' and spec.get('phase') == 'create'
                and spec['retry_setup_from'] == str(V100_A_SETUP_ATTEMPT)):
            allowed.add('retry_setup_from')
        payloads = {'tensor_sha256': V100_TENSOR_FILES, 'video_sha256': v100_media_probe.FILES}
        selected = payloads.keys() & spec.keys()
        if selected:
            if len(selected) != 1 or spec.get('phase') != 'gpu' or spec['fixture'] == 'arch-v100':
                raise ValueError('V100 supplement requires a dedicated GPU phase')
            key, = selected
            digests = spec[key]
            if (not isinstance(digests, dict) or set(digests) != set(payloads[key])
                    or any(not isinstance(digest, str) or not CID.fullmatch(digest)
                           for digest in digests.values())):
                raise ValueError('invalid V100 supplement probe digests')
            allowed.add(key)
        if (set(spec) != allowed
                or spec.get('phase') not in ('create', 'gpu')
                or type(spec.get('seconds')) is not int or not 1 <= spec['seconds'] <= 120
                or not isinstance(spec.get('unit'), str)
                or re.fullmatch(r'unifier-(?:v100-[ab]|arch-v100)-(?:create|gpu)-[a-z0-9-]{1,30}', spec['unit']) is None
                or not spec['unit'].startswith('unifier-'+spec['fixture']+'-'+spec['phase']+'-')):
            raise ValueError('invalid bounded V100 fixture phase')
        return FIXTURES[spec['fixture']]
    if not isinstance(spec, dict) or set(spec) - {'fixture', 'phase', 'unit', 'seconds',
                                                 'command', 'gpu_payload_bytes', 'acl_guard_unit',
                                                 'package_plan', 'resume_from', 'retry_from',
                                                 'validator_retry_from', 'supervisor_tasks',
                                                 'supervisor_memory_mb',
                                                 'recover_from', 'transaction', 'repair_from',
                                                 'bootstrap_from', 'guest_profile', 'intel_media_cache',
                                                 'arch_plan_sha256', 'controller', 'qq_gui', 'qq_recovery'}:
        raise ValueError('unknown hardware guard settings')
    if 'arch_plan_sha256' in spec and spec.get('fixture') not in ('arch-intel', 'arch-amd'):
        raise ValueError('Arch offline package plan requires the fixed Arch fixture')
    if not isinstance(spec.get('fixture'), str) or spec['fixture'] not in FIXTURES:
        raise ValueError('hardware fixture is outside the approved pair')
    phase = spec.get('phase')
    seconds = spec.get('seconds')
    unit = spec.get('unit')
    if (not isinstance(phase, str) or phase not in PHASES or type(seconds) is not int
            or not 1 <= seconds <= 300 or not isinstance(unit, str)
            or not re.fullmatch('unifier-drm-[a-z0-9-]{1,48}', unit)):
        raise ValueError('invalid hardware phase, unit or deadline')
    if phase == 'media' and (spec['fixture'] not in ('intel', 'arch-intel') or seconds > 120):
        raise ValueError('media phase requires the Intel fixture and 120 second deadline')
    qq_write_recovery = spec.get('qq_recovery') == 'txnb2fb18764986'
    if 'controller' in spec:
        if spec['fixture'] != 'intel' or not (phase == 'gpu' or qq_write_recovery):
            raise ValueError('an explicit controller budget requires the Intel graphics fixture')
        selected_controller_budget(spec)
    if 'qq_gui' in spec and (spec['qq_gui'] is not True or spec['fixture'] != 'intel'
            or spec.get('controller') not in (reuse.AUTHORIZED_CONTROLLER, QQ_STANDARD_CONTROLLER)
            or seconds > 180
            or not ((phase == 'gpu' and unit.startswith('unifier-drm-qq-gui-'))
                    or (phase == 'prepare' and qq_write_recovery and unit.startswith('unifier-drm-qq-recover-')))):
        raise ValueError('QQ GUI requires the Intel graphics fixture and a bounded private phase')
    if 'qq_recovery' in spec and (spec['qq_recovery'] not in ('txnb9f44de145d1', 'txnb2fb18764986')
            or spec['fixture'] != 'intel' or phase != 'prepare'
            or (qq_write_recovery and spec.get('qq_gui') is not True)
            or seconds > (180 if qq_write_recovery else 120)
            or not unit.startswith('unifier-drm-qq-recover-')):
        raise ValueError('QQ recovery requires the recorded Intel package transaction')
    if 'intel_media_cache' in spec and (
            spec.get('intel_media_cache') is not True or spec['fixture'] != 'intel' or phase != 'prepare'
            or 'package_plan' not in spec):
        raise ValueError('Intel media cache requires an explicit preparation package plan')
    acl = spec.get('acl_guard_unit')
    radeon = spec['fixture'] in ('amd', 'arch-amd')
    if spec['fixture'] == 'arch-amd':
        fields = {'fixture', 'phase', 'unit', 'seconds', 'supervisor_memory_mb', 'supervisor_tasks', 'acl_guard_unit'}
        extras = {'create': set(), 'prepare': {'command'}, 'gpu': {'command', 'gpu_payload_bytes'}}
        if phase == 'prepare' and 'arch_plan_sha256' in spec:
            fields.add('arch_plan_sha256')
            if not isinstance(spec['arch_plan_sha256'], str) or not CID.fullmatch(spec['arch_plan_sha256']):
                raise ValueError('invalid server Arch offline package plan digest')
        if (phase not in extras or set(spec) != fields | extras[phase] or seconds > 120
                or not spec['unit'].startswith('unifier-drm-arch-amd-'+phase+'-')
                or spec['supervisor_memory_mb'] != 96 or spec['supervisor_tasks'] != 20):
            raise ValueError('invalid server Arch AMD fixture phase')
    if radeon:
        if (seconds > 180 or not isinstance(acl, str)
                or not re.fullmatch(r'unifier-v008-amd-acl-[a-z0-9-]{1,40}\.service', acl)):
            raise ValueError('AMD phase requires the authorized finite root ACL watchdog')
    elif 'acl_guard_unit' in spec:
        raise ValueError('Intel does not use a root ACL watchdog')
    qq_monitor = spec['fixture'] == 'intel' and unit.startswith('unifier-drm-qq-')
    if 'supervisor_tasks' in spec and (not (radeon or qq_monitor)
            or type(spec['supervisor_tasks']) is not int or spec['supervisor_tasks'] != 20):
        raise ValueError('only the explicit 20-task AMD supervisor profile is supported')
    if 'supervisor_memory_mb' in spec and (not (radeon or qq_monitor)
            or spec.get('supervisor_tasks') != 20
            or type(spec['supervisor_memory_mb']) is not int or spec['supervisor_memory_mb'] != 96):
        raise ValueError('96 MiB AMD supervisor requires the explicit 20-task profile')
    if qq_monitor and ('supervisor_tasks' in spec) != ('supervisor_memory_mb' in spec):
        raise ValueError('the Intel QQ monitor requires both explicit budgets')
    if 'guest_profile' in spec:
        profile = spec['guest_profile']
        if (not isinstance(profile, str) or profile not in AMD_DNF_MEMORY_PROFILES
                or spec['fixture'] != 'amd' or phase != 'prepare'
                or spec.get('supervisor_tasks') != 20 or spec.get('supervisor_memory_mb') != 96):
            raise ValueError('AMD DNF memory profiles require the fixed AMD prepare budget')
        if profile == AMD_DNF_PROFILE and (
                'package_plan' in spec or spec.get('command') != AMD_DNF_RETRY_COMMAND):
            raise ValueError('AMD DNF query memory profile requires the fixed cache-only retry')
        if profile in AMD_DNF_INSTALL_PROFILES:
            command, _ = amd_install_controller(profile)
            if seconds != 180 or spec.get('command') != command or spec.get('package_plan') != AMD_DNF_EXACT_PLAN:
                raise ValueError('AMD DNF install memory profile requires the exact six-request offline plan')
    if phase == 'resume':
        if spec['fixture'] != 'intel' or spec.get('resume_from') != str(INTEL_RESUME_FROM):
            raise ValueError('only the recorded Intel creation attempt may be resumed')
        if 'retry_from' in spec and spec['retry_from'] != str(INTEL_RETRY_FROM):
            raise ValueError('only the Intel conmon locale failure may be retried')
        if 'validator_retry_from' in spec and (
                'retry_from' not in spec or spec['validator_retry_from'] != str(INTEL_VALIDATOR_RETRY_FROM)):
            raise ValueError('only the recorded Intel pre-start environment check may be retried')
    elif {'resume_from', 'retry_from', 'validator_retry_from'} & spec.keys():
        raise ValueError('resume evidence is admitted only in the Intel resume phase')
    if phase == 'amd-bootstrap-recover':
        if (spec['fixture'] != 'amd' or seconds > 120
                or spec.get('bootstrap_from') != str(AMD_BOOTSTRAP_FROM)
                or spec.get('supervisor_tasks') != 20 or spec.get('supervisor_memory_mb') != 96):
            raise ValueError('only the recorded AMD bootstrap may be completed under the verified budget')
    elif 'bootstrap_from' in spec:
        raise ValueError('old AMD bootstrap evidence is only used by its dedicated phase')
    if phase in PACKAGE_MAINTENANCE_PHASES:
        if (spec['fixture'] != 'intel'
                or spec.get('recover_from') != str(INTEL_RECOVER_FROM)
                or spec.get('transaction') != INTEL_RECOVER_TRANSACTION):
            raise ValueError('only the interrupted Intel fixture transaction may be recovered')
        if phase == 'recover' and (seconds > 120 or spec.get('repair_from') != str(INTEL_REPAIR_FROM)):
            raise ValueError('formal recovery requires the exact successful Intel native repair receipt')
        if phase == 'native-repair' and ('repair_from' in spec
                or spec.get('command') != [PYTHON, str(INTEL_REPAIR_CONTROLLER)]):
            raise ValueError('native repair requires the exact Intel controller')
    elif {'recover_from', 'transaction', 'repair_from'} & spec.keys():
        raise ValueError('recovery evidence is admitted only in the Intel package maintenance phases')
    if phase in BUILTIN_PHASES:
        if 'command' in spec:
            raise ValueError('this phase uses the built-in formal service entry only')
    else:
        command = spec.get('command')
        if (not isinstance(command, list) or not 1 <= len(command) <= 32
                or any(not isinstance(arg, str) or '\0' in arg or len(arg) > 4096
                       for arg in command) or command[0] != PYTHON):
            raise ValueError('reuse requires an explicit Python controller argv')
    if phase == 'gpu':
        payload = spec.get('gpu_payload_bytes')
        if type(payload) is not int or not 1 <= payload <= (64 if spec.get('qq_gui') else 16)*MIB:
            raise ValueError('invalid GPU payload budget')
    elif 'gpu_payload_bytes' in spec:
        raise ValueError('GPU payload is only admitted during the dedicated GPU phase')
    if 'package_plan' in spec:
        plan = spec['package_plan']
        if spec['fixture'] == 'amd' and spec.get('guest_profile') not in AMD_DNF_INSTALL_PROFILES:
            raise ValueError('an AMD package plan requires the reviewed offline installation profile')
        if (phase != 'prepare' or not isinstance(plan, dict)
                or set(plan) != {'requests', 'download_bytes', 'installed_bytes', 'signed_repository'}
                or type(plan['download_bytes']) is not int or not 0 < plan['download_bytes'] <= 200*MIB
                or type(plan['installed_bytes']) is not int or not 0 < plan['installed_bytes'] <= 1024*MIB
                or not isinstance(plan['signed_repository'], str) or not plan['signed_repository']
                or not isinstance(plan['requests'], list) or not 1 <= len(plan['requests']) <= 32):
            raise ValueError('a package preparation plan needs exact native requests, repository and size bounds')
        package_names(spec['fixture'], plan['requests'])
        if (spec.get('intel_media_cache') and (
                package_names('intel', plan['requests']) != list(intel_media_guard.PACKAGES)
                or plan['signed_repository'] != intel_media_guard.DEBIAN_RELEASE
                or plan['download_bytes'] + 48*MIB > 200*MIB
                or plan['download_bytes'] + plan['installed_bytes'] + 48*MIB > 1024*MIB)):
            raise ValueError('Intel media preparation exceeds the fixed signed package closure or budgets')
    return FIXTURES[spec['fixture']]


def trusted_fixture_path(path, fixture):
    if (path.name != 'spec.json' or path != path.resolve(strict=True)
            or path.parent.is_relative_to(fixture.root) or path.parent.is_relative_to(fixture.shared)):
        raise RuntimeError('spec and receipts must stay outside the guest and its share')
    owned_directory(path.parent, private=True)


def snapshot(timeout_limit=None):
    def native(argv):
        if timeout_limit is None:
            return reuse.call(argv)
        return reuse.call(argv, timeout=timeout_limit(5))

    output = native(['podman', 'ps', '--all', '--quiet', '--no-trunc'])
    ids = output.splitlines()
    if len(ids) > 64 or len(ids) != len(set(ids)) or not all(CID.fullmatch(cid) for cid in ids):
        raise RuntimeError('native container inventory exceeds budget or lacks complete IDs')
    if not ids:
        return {}
    # Query a deliberately small field set, including all restart indicators.
    result = {}
    for batch in range(0, len(ids), 8):
        entries = ids[batch:batch+8]
        rows = json.loads(native(['podman', 'container', 'inspect', '--format', 'json', *entries]))
        if {r.get('Id') for r in rows} != set(entries) or len(rows) != len(entries):
            raise RuntimeError('native container inventory changed during snapshot')
        for row in rows:
            state = row['State']
            result[row['Id']] = {'name': row['Name'], 'labels': row['Config'].get('Labels') or {},
                                 'state': {key: state.get(key) for key in
                                           ('Status', 'Running', 'Pid', 'StartedAt', 'FinishedAt',
                                            'ExitCode', 'OOMKilled', 'Restarting')}}
    return result


def fixture_create_spec(fixture):
    mounts = () if fixture.cuda else ((str(fixture.shared), str(fixture.shared)),)
    return CreateSpec(name=fixture.name, env_id=fixture.env_id, project=str(fixture.root),
                      image=fixture.reference,
                      mounts=mounts, network=fixture.network, devices=fixture.devices, cuda=fixture.cuda)


def fixture_create_argv(fixture, hooks, spec):
    argv = creation_argv(spec)
    argv[1:1] = ['--hooks-dir='+str(hooks)]
    argv[3:3] = [
        '--cgroup-parent='+fixture.slice, '--cgroups=enabled', '--memory=402653184',
        '--memory-swap=536870912', '--pids-limit=128', '--cpus=1',
        '--cgroupns=private', '--ipc=private', '--pid=private', '--userns=host', '--user=0:0',
    ]
    if fixture.cuda:
        extra_volumes = ['--volume', f'{V100_INPUTS[V100_PROBE][0]}:{V100_PROBE}:ro,nosuid,nodev',
                         '--volume', f'{V100_INPUTS[V100_CUBIN][0]}:{V100_CUBIN}:ro,nosuid,nodev,noexec']
        argv[-2:-2] = extra_volumes
    return argv


def register_intent(path, evidence):
    # Preserve a failed or interrupted attempt, including a partial write.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(evidence, stream)
        stream.flush()
        os.fsync(stream.fileno())


class HardwareAttempt:
    tasks = {'aggregate': 256, 'guest': 128}
    entry = ENTRY

    def __init__(self, path):
        self.fixture = fixture = validate_spec(spec := bound_read(path))
        trusted_fixture_path(path, fixture)
        if (os.getuid() != fixture.uid or os.geteuid() != fixture.uid
                or pwd.getpwuid(fixture.uid).pw_name != fixture.owner):
            raise RuntimeError('fixture needs its approved rootless owner')
        self.spec = spec
        self.controller_budget = selected_controller_budget(spec)
        self.out = path.parent
        self.base = fixture.root.parent
        self.phase = spec['phase']
        self.binding_path = self.base / 'fixture-binding.json'
        self.once = self.base / 'create-attempt.json'
        if 'retry_setup_from' in spec:
            self.once = self.base / 'create-attempt-after-source-fix.json'
        self.lockpath = self.base / 'test-guard.lock'
        self.hooks = self.base / 'empty-hooks'
        self.record_path = fixture.root / '.lab/state/environments' / (fixture.env_id+'.json')
        self.resources = {'slice': fixture.slice, 'memory': 384*MIB, 'memory_swap': 512*MIB}
        if spec.get('qq_gui'):
            self.aggregate_memory = 1536*MIB
            self.tasks = {'aggregate': 384, 'guest': 128}
        amd_memory = spec.get('supervisor_memory_mb', 48)
        self.monitor_budget = ({'memory_max': amd_memory*MIB,
                                'memory_high': (80 if amd_memory == 96 else 36)*MIB,
                                'tasks_max': spec.get('supervisor_tasks', 12)}
                               if fixture.driver == 'radeon' else
                               {'memory_max': 64*MIB, 'memory_high': 48*MIB, 'tasks_max': 16})
        if spec.get('supervisor_memory_mb') == 96 and fixture.driver == 'i915':
            self.monitor_budget = {'memory_max': 96*MIB, 'memory_high': 80*MIB, 'tasks_max': 20}
        self.admission_monitor_bytes = self.monitor_budget['memory_max']
        if fixture.cuda:
            self.monitor_budget = {'memory_max': 96*MIB, 'memory_high': 80*MIB, 'tasks_max': 20}
            self.admission_monitor_bytes = self.monitor_budget['memory_max']
        if fixture.driver == 'radeon':
            self.admission_monitor_bytes += 16*MIB
        self.config = {**spec, 'project': str(fixture.root), 'env_id': fixture.env_id,
                       'shared': str(fixture.shared), 'cid': None, 'container_name': fixture.name,
                       'private_root': str(self.out / 'private'), 'image_lock': str(LOCK),
                       'reserve_bytes': 1024*MIB}
        if self.phase in BUILTIN_PHASES:
            self.config['command'] = [PYTHON, str(ENTRY), self.phase+'-worker', str(path)]
        if fixture.cuda:
            self.config['mounts'] = [{'source': str(source), 'destination': destination}
                                     for destination, (source, _) in V100_INPUTS.items()]
            if self.phase == 'gpu':
                self.config['command'] = [PYTHON, str(ENTRY), 'cuda-worker', str(path)]
        self.create_event = None
        self.start_seen = False
        self.guest_policy = None
        self.sample_roots = ()
        self.acl_watch = None

    def prepare_base(self):
        if not os.path.lexists(self.base):
            self.base.mkdir(mode=0o700)
        owned_directory(self.base, private=True)

    def preflight(self, monitor, aggregate, actual):
        if (reuse.under(monitor['cgroup'], aggregate)
                or reuse.unit(self.spec['unit']+'-guard.service').get('ControlGroup') != monitor['cgroup']):
            raise RuntimeError('supervisor is outside its independent registered unit')
        bound = reuse.limits(monitor['cgroup'])
        if not cpu_pinned(bound) or not cpu_pinned(actual):
            raise RuntimeError('CPU/swap/task supervision budget differs')
        reuse.assert_controller_limits(monitor['cgroup'], self.monitor_budget)
        if self.fixture.driver == 'radeon':
            self.acl_watch = self.read_acl_watch()
            self.check_acl_watch(self.spec['seconds']+90)
            reuse.put(self.out/'acl-watchdog.json', self.acl_watch)
        reuse.put(self.out/'non-target-before.json', self.check_preconditions())
        if self.fixture.cuda:
            verify_cuda_inputs(self.fixture)
            if 'tensor_sha256' in self.spec:
                self.cuda_payload('tensor')
            if 'video_sha256' in self.spec:
                self.cuda_payload('video')
            reuse.put(self.out/'gpu-admission.json', self.cuda_telemetry())
        elif self.phase == 'vulkan':
            self.vulkan_binary()
            reuse.put(self.out/'gpu-admission.json', observe_gpu(self.fixture, vulkan_probe.PAYLOAD_BYTES))
        elif self.phase == 'media':
            reuse.put(self.out/'gpu-admission.json', observe_gpu(self.fixture, intel_media_guard.PAYLOAD_BYTES))
        elif self.phase == 'gpu':
            reuse.put(self.out/'gpu-admission.json', observe_gpu(self.fixture, self.spec['gpu_payload_bytes']))
        node = observe_device(self.fixture)
        reuse.put(self.out/'device-before.json', node)
        engine = self.engine()
        self.sample_roots = (self.out, self.base, Path(engine['store']['graphRoot']))
        self.check_disk(initial=True)

    def read_acl_watch(self):
        name = self.spec['acl_guard_unit']
        fields = ('Id', 'ActiveState', 'MainPID', 'InvocationID', 'ControlGroup', 'RuntimeMaxUSec',
                  'ActiveEnterTimestampMonotonic')
        raw = reuse.call(['systemctl', '--system', 'show', name,
                          *('-p'+field for field in fields)], timeout=2)
        unit = dict(line.split('=', 1) for line in raw.splitlines())
        if (unit.get('Id') != name or unit.get('ActiveState') != 'active'
                or not re.fullmatch(r'[0-9a-f]{32}', unit.get('InvocationID', ''))
                or not unit.get('ActiveEnterTimestampMonotonic', '').isdigit()
                or unit.get('RuntimeMaxUSec') not in {'5min', '300s', '300000000'}
                or unit.get('ControlGroup') != '/system.slice/'+name):
            raise RuntimeError('AMD root ACL watchdog is unavailable')
        process = reuse.process(int(unit['MainPID']))
        root = Path('/proc') / unit['MainPID']
        # stat of the proc directory can be changed by process dumpability.
        # The real/effective/saved/filesystem UIDs must all be root instead.
        owner = next(row.split()[1:] for row in (root/'status').read_text().splitlines() if row.startswith('Uid:'))
        actual = reuse.limits(process['cgroup'])
        if (owner != ['0']*4 or process['cgroup'] != unit['ControlGroup']
                or not cpu_pinned(actual)):
            raise RuntimeError('AMD ACL watchdog owner or scope differs')
        reuse.assert_controller_limits(process['cgroup'],
                                       {'memory_max': 16*MIB, 'memory_high': 12*MIB, 'tasks_max': 4})
        return {'process': process, 'unit': name, 'invocation_id': unit['InvocationID'],
                'deadline': int(unit['ActiveEnterTimestampMonotonic'])/1_000_000+300}

    def check_acl_watch(self, required=ACL_QUEUE_RESERVE, *, reread=False):
        if self.fixture.driver != 'radeon':
            return
        expected = self.acl_watch or bound_read(self.out/'acl-watchdog.json')
        if expected['deadline']-time.monotonic() < required:
            (self.out/'queue-closed').touch()
            raise RuntimeError('AMD ACL watchdog has insufficient remaining lease')
        if (not reuse.same_execution(expected['process'], reuse.process(expected['process']['pid']))
                or (reread and self.read_acl_watch() != expected)):
            (self.out/'queue-closed').touch()
            raise RuntimeError('AMD ACL watchdog identity changed')

    def cleanup_timeout(self, requested, *, guest=False):
        # Keep enough time to stop and confirm the separately registered worker,
        # including when the target's own cleanup cannot establish identity.
        holdback = ACL_WORKER_CLEANUP if guest else 0
        return command_timeout(self, requested, cleanup=True, holdback=holdback)

    def engine(self):
        engine = json.loads(reuse.call(['podman', 'info', '--format', 'json']))
        if (engine['host']['security']['rootless'] is not True
                or engine['host']['cgroupVersion'] != 'v2'
                or engine['host']['cgroupManager'] != 'systemd'
                or engine['host'].get('eventLogger') in (None, '', 'none')):
            raise RuntimeError('rootless Podman with cgroup v2/systemd and events is required')
        return engine

    def verify_cached_image(self):
        image_command = ['podman', 'image', 'inspect', '--format', 'json']
        fixture = self.fixture
        by_reference, = json.loads(reuse.call([*image_command, fixture.reference]))
        by_config_id, = json.loads(reuse.call([*image_command, fixture.image_id]))
        reference_digest = validate_image(fixture, by_reference)
        canonical_digest = validate_image(fixture, by_config_id, by_config_id=True)
        proof = {'reference': fixture.reference, 'config_id': fixture.image_id,
                 'reference_digest': reference_digest, 'container_digest': canonical_digest,
                 'config_id_repo_digests': sorted(set(by_config_id['RepoDigests']))}
        evidence = self.out/'image-before.json'
        if evidence.exists():
            if proof != bound_read(evidence):
                raise RuntimeError('cached image fingerprint changed after admission')
        else:
            reuse.put(evidence, proof)

    def arch_package_plan(self):
        expected = self.spec.get('arch_plan_sha256')
        if self.spec['fixture'] not in ('arch-intel', 'arch-amd') or self.phase != 'prepare' or not expected:
            raise RuntimeError('only the exact Arch preparation phase admits this package plan')
        data = bound_bytes(self.out.parent/'package-plan.json')
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('Arch package plan hash changed')
        plan = arch_gpu_packages.validate_plan(json.loads(data), self.active_config()['cid'])
        if (plan['mode'] == 'amd') != (self.spec['fixture'] == 'arch-amd'):
            raise RuntimeError('Arch package plan belongs to another GPU fixture')
        return plan

    def verify_resume_provenance(self, current):
        old = INTEL_RESUME_FROM
        cid = INTEL_RESUME_CID
        fixture = self.fixture
        owned_directory(old, private=True)
        for name in ('abort.json', 'start-intent.json', 'live-binding.json', 'official-binding.json'):
            absent(old/name)
        absent(self.resume_marker())
        absent(self.binding_path)
        absent(self.record_path)
        absent(fixture.root/'.lab/leases'/(fixture.env_id+'.json'))
        old_spec = bound_read(old/'spec.json')
        original = bound_read(self.once)
        if (validate_spec(old_spec) != fixture or old_spec['phase'] != 'create'
                or original.get('attempt') != str(old) or original.get('fixture') != 'intel'
                or not reuse.same_execution(original.get('owner'), bound_read(old/'command-identity.json'))):
            raise RuntimeError('original create intent is not bound to the approved Intel attempt')
        intent = bound_read(old/'create-intent.json')
        event = bound_read(old/'create-event.json')
        observed_events = [json.loads(line) for line in bound_bytes(old/'events.jsonl').splitlines()]
        command = bound_read(old/'command-result.json')
        receipt = bound_read(old/'receipt.json')
        if (bound_read(old/'created-container.json') != {'cid': cid}
                or intent.get('argv') != fixture_create_argv(fixture, self.hooks, fixture_create_spec(fixture))
                or observed_events != [event] or event.get('ID') != cid or event.get('Name') != fixture.name
                or event.get('Image') != fixture.reference or event.get('Status') != 'create'
                or event.get('Type') != 'container' or not matches_labels(fixture, event.get('Attributes'))
                or not intent['at_ns'] <= event['timeNano'] <= command['at']*1_000_000_000
                or command.get('exit_code') != 2 or receipt.get('reason') is not None
                or receipt.get('command_result') != command
                or receipt.get('cleanup', {}).get('status') != 'NO_START'
                or bound_read(old/'rescue.json').get('status') != 'NO_START'
                or bound_bytes(old/'command.log').strip()
                    != b'container identity or image differs from the approved fixture'):
            raise RuntimeError('original creation evidence is incomplete or indicates an unauthorized recovery')
        before = bound_read(old/'non-target-before.json')
        after = bound_read(old/'native-after.json')
        untouched = {'Status': 'created', 'Running': False, 'Pid': 0,
                     'StartedAt': '0001-01-01T00:00:00Z', 'FinishedAt': '0001-01-01T00:00:00Z',
                     'ExitCode': 0, 'OOMKilled': False, 'Restarting': False}
        if (cid in before or after.get(cid) != {'name': fixture.name, 'state': untouched, 'labels': fixture.labels}
                or {key: value for key, value in after.items() if key != cid} != before or current != after):
            raise RuntimeError('target was started or non-target state differs from the original attempt')
        proof = {'cid': cid, 'original_attempt': str(old), 'original_create_event': event,
                 'original_command': command}
        if 'retry_from' in self.spec:
            proof['locale_retry'] = self.verify_locale_retry(current, proof)
        if 'validator_retry_from' in self.spec:
            proof['validator_retry'] = self.verify_validator_retry(current, proof)
        return proof

    def resume_marker(self):
        if 'validator_retry_from' in self.spec:
            return self.base/'resume-environment-retry-attempt.json'
        name = 'resume-locale-retry-attempt.json' if 'retry_from' in self.spec else 'resume-attempt.json'
        return self.base/name

    def verify_locale_retry(self, current, initial_proof):
        failed = INTEL_RETRY_FROM
        owned_directory(failed, private=True)
        for name in ('abort.json', 'live-binding.json', 'official-binding.json', 'create-intent.json'):
            absent(failed/name)
        spec = bound_read(failed/'spec.json')
        marker = bound_read(self.base/'resume-attempt.json')
        command = bound_read(failed/'command-result.json')
        receipt = bound_read(failed/'receipt.json')
        start = bound_read(failed/'start-intent.json')
        log = (
            'podman start failed: conmon: option parsing failed: Invalid byte sequence in conversion input\n'
            f'Error: unable to start container "{INTEL_RESUME_CID}": {INTEL_RETRY_START_ERROR}'
        )
        before = start.get('before', {})
        if (validate_spec(spec) != self.fixture or spec['phase'] != 'resume' or 'retry_from' in spec
                or marker.get('attempt') != str(failed)
                or marker.get('original_attempt') != str(INTEL_RESUME_FROM)
                or marker.get('cid') != INTEL_RESUME_CID
                or not reuse.same_execution(marker.get('owner'), bound_read(failed/'command-identity.json'))
                or bound_read(failed/'resume-proof.json') != initial_proof
                or start.get('cid') != INTEL_RESUME_CID or start['at_ns'] > command['at']*1_000_000_000
                or before.get('Status') != 'created' or before.get('Running') is not False
                or before.get('Pid') != 0 or before.get('Error') != ''
                or before.get('StartedAt') != '0001-01-01T00:00:00Z'
                or before.get('FinishedAt') != '0001-01-01T00:00:00Z'
                or command.get('exit_code') != 2 or receipt.get('reason') is not None
                or receipt.get('command_result') != command
                or receipt.get('cleanup', {}).get('status') != 'NO_START'
                or bound_read(failed/'rescue.json').get('status') != 'NO_START'
                or bound_bytes(failed/'events.jsonl').strip()
                or bound_bytes(failed/'command.log').decode('utf-8').strip() != log
                or bound_read(failed/'non-target-before.json') != current
                or bound_read(failed/'native-after.json') != current):
            raise RuntimeError('Intel retry requires the recorded no-start conmon locale failure')
        return {'attempt': str(failed), 'command': command, 'start_intent_at_ns': start['at_ns'],
                'expected_error': INTEL_RETRY_START_ERROR}

    def verify_validator_retry(self, current, locale_retry_proof):
        failed = INTEL_VALIDATOR_RETRY_FROM
        owned_directory(failed, private=True)
        for name in ('abort.json', 'start-intent.json', 'live-binding.json', 'official-binding.json',
                     'create-intent.json', 'created-container.json'):
            absent(failed/name)
        spec = bound_read(failed/'spec.json')
        marker = bound_read(self.base/'resume-locale-retry-attempt.json')
        command = bound_read(failed/'command-result.json')
        receipt = bound_read(failed/'receipt.json')
        cleanup = receipt.get('cleanup', {})
        worker = cleanup.get('worker_final', {})
        if (validate_spec(spec) != self.fixture or spec['phase'] != 'resume'
                or spec.get('retry_from') != str(INTEL_RETRY_FROM) or 'validator_retry_from' in spec
                or marker.get('attempt') != str(failed)
                or marker.get('original_attempt') != str(INTEL_RESUME_FROM)
                or marker.get('cid') != INTEL_RESUME_CID
                or not reuse.same_execution(marker.get('owner'), bound_read(failed/'command-identity.json'))
                or bound_read(failed/'resume-proof.json') != locale_retry_proof
                or command.get('exit_code') != 2 or receipt.get('reason') is not None
                or receipt.get('command_result') != command or cleanup.get('status') != 'NO_START'
                or worker.get('MainPID') != '0' or worker.get('ControlGroup') != ''
                or bound_read(failed/'cleanup.json') != cleanup or bound_read(failed/'rescue.json') != cleanup
                or bound_bytes(failed/'events.jsonl').strip()
                or bound_bytes(failed/'command.log').strip() != b'resume-create environment differs from the cached image'
                or bound_read(failed/'non-target-before.json') != current
                or bound_read(failed/'native-after.json') != current):
            raise RuntimeError('Intel validator retry requires the recorded pre-start environment failure')
        return {'attempt': str(failed), 'command': command}

    def verify_amd_bootstrap_provenance(self, current):
        old = AMD_BOOTSTRAP_FROM
        cid = AMD_BOOTSTRAP_CID
        f = self.fixture
        owned_directory(old, private=True)
        for path in (self.base/AMD_BOOTSTRAP_MARKER, self.binding_path, self.record_path,
                     EnvironmentStore(f.root).inventory_path(f.env_id),
                     f.root/'.lab/leases'/(f.env_id+'.json'),
                     old/'official-binding.json', old/'create-event.json'):
            absent(path)
        store = EnvironmentStore(f.root)
        if store.transactions_for(f.env_id) or store.launches_for(f.env_id):
            raise RuntimeError('AMD bootstrap already has package or launch history')
        spec = bound_read(old/'spec.json')
        once = bound_read(self.once)
        create = bound_read(old/'create-intent.json')
        start = bound_read(old/'start-intent.json')
        abort = bound_read(old/'abort.json')
        command = bound_read(old/'command-result.json')
        receipt = bound_read(old/'receipt.json')
        clean = bound_read(old/'cleanup.json')
        live = bound_read(old/'live-binding.json')
        ready = bound_read(old/'ready.json')
        old_before = bound_read(old/'non-target-before.json')
        old_after = bound_read(old/'native-after.json')
        stopped = clean.get('state', {})
        worker = clean.get('worker_final', {})
        aggregate = ready['aggregate']
        former = start['before']
        if (validate_spec(spec) != f or spec['phase'] != 'create'
                or self.spec['acl_guard_unit'] == spec['acl_guard_unit'] or self.spec['unit'] == spec['unit']
                or once.get('attempt') != str(old) or once.get('fixture') != 'amd'
                or not reuse.same_execution(once.get('owner'), bound_read(old/'command-identity.json'))
                or create.get('argv') != fixture_create_argv(f, self.hooks, fixture_create_spec(f))
                or bound_read(old/'created-container.json') != {'cid': cid} or start.get('cid') != cid
                or former.get('Status') != 'created' or former.get('Running') is not False
                or former.get('Pid') != 0 or former.get('StartedAt') != '0001-01-01T00:00:00Z'
                or former.get('FinishedAt') != '0001-01-01T00:00:00Z'
                or abort.get('reason') != 'unexpected create event: '+cid
                or receipt.get('reason') != abort['reason'] or receipt.get('cleanup') != clean
                or receipt.get('command_result') != command or command.get('exit_code') != 2
                or bound_bytes(old/'command.log').strip() != b'cannot bind a stopped execution'
                or bound_read(old/'rescue.json') != clean or clean.get('status') != 'STOPPED'
                or clean.get('cid') != cid or worker.get('MainPID') != '0' or worker.get('ControlGroup') != ''
                or stopped.get('Status') != 'exited' or stopped.get('Running') is not False
                or stopped.get('Pid') != 0 or stopped.get('ExitCode') != 143
                or stopped.get('OOMKilled') is not False or stopped.get('StoppedByUser') is not True
                or live.get('cid') != cid or live.get('coverage_verified') is not True
                or live.get('started_at') != stopped.get('StartedAt')
                or live['init']['boot_id'] != reuse.process(os.getpid())['boot_id']
                or live['scope'].get('Id') != 'libpod-'+cid+'.scope'
                or not live['scope'].get('InvocationID')
                or not reuse.under(live['init']['cgroup'], live['scope']['ControlGroup'])
                or not reuse.under(live['init']['cgroup'], aggregate)
                or not reuse.under(live['conmon']['cgroup'], aggregate)
                or live['guest_limits'].get('memory.max') != 384*MIB
                or live['guest_limits'].get('memory.swap.max') != 128*MIB
                or live['guest_limits'].get('pids.max') != 128
                or bound_read(old/'guest-device.json') != {'mapping': ['0', '1001', '1'], 'rdev': os.makedev(226, 128)}
                or bound_read(old/'image-before.json') != bound_read(self.out/'image-before.json')
                or bound_read(old/'device-before.json') != observe_device(f)
                or current != old_after or cid in old_before or cid not in current
                or current[cid]['name'] != f.name or not matches_labels(f, current[cid]['labels'])
                or {key: value for key, value in old_after.items() if key != cid} != old_before
                or any(current[cid]['state'].get(key) != stopped.get(key)
                       for key in ('Status', 'Running', 'Pid', 'StartedAt', 'FinishedAt', 'ExitCode', 'OOMKilled'))):
            raise RuntimeError('AMD bootstrap provenance or original stopped execution differs')
        old_events = [json.loads(line) for line in bound_bytes(old/'events.jsonl').splitlines()]
        since = bound_read(old/'event-since.json')['since']
        if since != '2026-11-15T12:11:52Z':
            raise RuntimeError('AMD bootstrap event selection differs from the original attempt')
        raw = reuse.call(['podman', 'events', '--stream=false', '--since', since,
                          '--filter', 'type=container', '--filter', 'container='+cid,
                          '--format', 'json'], timeout=10)
        if len(raw) > 65536:
            raise RuntimeError('AMD bootstrap native history exceeds its bound')
        history = [json.loads(line) for line in raw.splitlines()]
        validate_amd_bootstrap_history(f, cid, old_events, history, create_intent=create['at_ns'],
                                       start_intent=start['at_ns'], aborted=int(abort['at']*1_000_000_000),
                                       stopped=stopped, command_end=int(command['at']*1_000_000_000))
        return {'cid': cid, 'original_attempt': str(old), 'native_history': history,
                'stopped': stopped, 'abort': abort, 'old_init': live['init']}

    def pending_transactions(self):
        service = self.service()
        record = bound_read(self.record_path)
        return record, service._pending_package_transactions(self.fixture.env_id)

    def verify_arch_maintenance(self, observed):
        phase = self.phase
        controller = Path(self.spec['command'][1])
        previous = ARCH_EVIDENCE/'install-graphics'
        abort = bound_read(previous/'receipt.json')
        txn = self.fixture.root/'.lab/state/transactions'/(ARCH_RECOVERY_TXN+'.json')
        transaction = bound_bytes(txn)
        if (self.out != ARCH_EVIDENCE/('recovery-'+phase)
                or hashlib.sha256(bound_bytes(controller)).hexdigest() != self.spec['controller_sha256']
                or hashlib.sha256(transaction).hexdigest() != ARCH_TRANSACTION_SHA
                or self.active_config()['cid'] != ARCH_RECOVERY_CID
                or binding_for(bound_read(self.record_path)) != json.loads(transaction)['environment_binding']
                or abort.get('reason') != 'GUEST_INVALID_HARD_LIMIT'
                or abort.get('cleanup', {}).get('cid') != ARCH_RECOVERY_CID
                or abort['cleanup'].get('status') != 'STOPPED'):
            raise RuntimeError('Arch maintenance provenance differs from the approved interruption')
        prior = abort
        if phase == 'replay-llvm':
            earlier = ARCH_EVIDENCE/'recovery-replay'
            prior = bound_read(earlier/'receipt.json')
            result = bound_read(earlier/'result.json')
            if (prior.get('reason') is not None or (prior.get('command_result') or {}).get('exit_code') != 1
                    or result.get('cid') != ARCH_RECOVERY_CID
                    or 'llvm-libs-23.1.1-1/desc: No such file' not in result.get('error', '')
                    or os.path.lexists(earlier/'lock-move-intent.json')):
                raise RuntimeError('LLVM isolation requires the exact stopped pre-lock failure')
        if phase == 'reconcile':
            earlier = ARCH_EVIDENCE/'recovery-replay-llvm'
            prior = bound_read(earlier/'receipt.json')
            result = bound_read(earlier/'result.json')
            if (prior.get('reason') is not None or (prior.get('command_result') or {}).get('exit_code') != 0
                    or result.get('status') != 'PASS' or result.get('cid') != ARCH_RECOVERY_CID):
                raise RuntimeError('Arch reconciliation requires a verified native replay')
        proof = prior['cleanup']['state']
        if (prior['cleanup']['cid'] != ARCH_RECOVERY_CID or observed.get('Running') or observed.get('Pid') != 0
                or any(observed.get(key) != proof.get(key) for key in ('Status', 'StartedAt', 'FinishedAt'))):
            raise RuntimeError('Arch stopped execution changed since the recovery evidence')

    def require_clean_packages(self):
        record, pending = self.pending_transactions()
        if record.get('package_recovery_required') or pending:
            raise RuntimeError('formal package recovery is required before prepare or GPU')

    def verify_amd_dnf_retry(self, current):
        original = AMD_DNF_ABORT_FROM
        cid = AMD_BOOTSTRAP_CID
        owned_directory(original, private=True)
        absent(self.base/AMD_DNF_RETRY_MARKER)
        if hashlib.sha256(bound_bytes(Path(AMD_DNF_RETRY_COMMAND[2]))).hexdigest() != AMD_DNF_RETRY_CONTROLLER_SHA256:
            raise RuntimeError('AMD DNF retry controller differs from the reviewed source')
        record = bound_read(self.record_path)
        store = EnvironmentStore(self.fixture.root)
        if (binding_for(record) != AMD_DNF_BINDING or store.transactions_for(self.fixture.env_id)
                or store.launches_for(self.fixture.env_id) or record.get('package_recovery_required')):
            raise RuntimeError('AMD DNF retry requires the exact unchanged fixture without package or launch history')
        spec = bound_read(original/'spec.json')
        abort = bound_read(original/'abort.json')
        receipt = bound_read(original/'receipt.json')
        cleanup = bound_read(original/'cleanup.json')
        command = bound_read(original/'command-result.json')
        live = bound_read(original/'live-binding.json')
        old_before = bound_read(original/'non-target-before.json')
        old_after = bound_read(original/'native-after.json')
        stopped = cleanup.get('state', {})
        worker = cleanup.get('worker_final', {})
        if (validate_spec(spec) != self.fixture or spec['phase'] != 'prepare'
                or spec.get('command') != AMD_DNF_ABORT_COMMAND or 'guest_profile' in spec
                or self.spec['unit'] == spec['unit'] or self.spec['acl_guard_unit'] == spec['acl_guard_unit']
                or abort.get('reason') != 'GUEST_INVALID_HARD_LIMIT' or receipt.get('reason') != abort['reason']
                or command.get('exit_code') != 1 or receipt.get('command_result') != command
                or cleanup.get('status') != 'STOPPED' or cleanup.get('cid') != cid
                or receipt.get('cleanup') != cleanup or bound_read(original/'rescue.json') != cleanup
                or stopped.get('Status') != 'exited' or stopped.get('Running') is not False
                or stopped.get('Pid') != 0 or stopped.get('OOMKilled') is not False
                or worker.get('MainPID') != '0' or worker.get('ControlGroup') != ''
                or live.get('cid') != cid or live.get('coverage_verified') is not True
                or live.get('started_at') != stopped.get('StartedAt')
                or live['init']['boot_id'] != reuse.process(os.getpid())['boot_id']
                or current != old_after or cid not in current
                or any(current[cid]['state'].get(key) != stopped.get(key)
                       for key in ('StartedAt', 'FinishedAt', 'Running', 'Pid', 'Status'))
                or {key: value for key, value in old_after.items() if key != cid}
                   != {key: value for key, value in old_before.items() if key != cid}):
            raise RuntimeError('AMD DNF retry does not match the recorded stopped hard-limit attempt')
        amd_dnf_effective_limits(live['guest_limits'], live['scope']['ControlGroup'])
        result = bound_read(original/'result.json')
        rows = result['commands']
        imports = [row for row in rows if row.get('label') == 'import-verified-cache']
        solvers = [row for row in rows if row.get('label') == 'dnf-solver']
        manifest = bound_bytes(original/'03-import-verified-cache.stdout')
        files = [line.split() for line in manifest.decode('ascii').splitlines()]
        if (result.get('fixture') != 'amd' or result.get('mode') != 'query'
                or result.get('cid') != cid or result.get('status') != 'INVALID'
                or len(imports) != 1 or imports[0].get('exit_code') != 0
                or imports[0].get('stdout_bytes') != len(manifest)
                or len(files) != 12 or any(len(fields) != 3 or not fields[1].isdigit()
                                          or not CID.fullmatch(fields[2]) for fields in files)
                or len({fields[0] for fields in files}) != 12
                or sum(int(fields[1]) for fields in files) != 49794425
                or result.get('stage_marker_sha256')
                    != 'e60461a1eb726cd527e418d7cf5dd700d6440c87890ddde3b930a8d4c73ca7a6'
                or len(solvers) != 1 or solvers[0].get('exit_code') != 137
                or '--cacheonly' not in solvers[0].get('argv', [])
                or '--assumeno' not in solvers[0].get('argv', [])):
            raise RuntimeError('AMD DNF retry requires the verified cache import and original bounded solver abort')
        return {'original': str(original), 'binding': AMD_DNF_BINDING,
                'before': live['guest_limits'], 'stopped': stopped,
                'cache_import_sha256': hashlib.sha256(manifest).hexdigest(),
                'original_abort_sha256': hashlib.sha256(bound_bytes(original/'abort.json')).hexdigest()}

    def claim_amd_dnf_retry(self):
        proof = bound_read(self.out/'amd-dnf-retry-before.json')
        if self.check_preconditions() != bound_read(self.out/'non-target-before.json'):
            raise RuntimeError('AMD native inventory changed before the one permitted retry')
        register_intent(self.base/AMD_DNF_RETRY_MARKER,
                        {'attempt': str(self.out), 'binding': AMD_DNF_BINDING,
                         'original_abort_sha256': proof['original_abort_sha256'],
                         'owner': reuse.process(os.getpid())})

    def verify_amd_install_query(self, current):
        source = AMD_DNF_SUCCEEDED_QUERY
        owned_directory(source, private=True)
        profile = self.spec.get('guest_profile')
        retry = profile != AMD_DNF_INSTALL_PROFILE
        absent(self.base/amd_install_marker(profile))
        absent(source/'abort.json')
        record = bound_read(self.record_path)
        store = EnvironmentStore(self.fixture.root)
        transactions = store.transactions_for(self.fixture.env_id)
        if (binding_for(record) != AMD_DNF_BINDING
                or [txn['id'] for txn in transactions] != ([AMD_DNF_INSTALL_TRANSACTION] if retry else [])
                or store.launches_for(self.fixture.env_id) or record.get('package_recovery_required')):
            raise RuntimeError('AMD install requires the unchanged fixture without transaction or launch history')
        if retry:
            if profile == AMD_DNF_INSTALL_REFRESH_PROFILE:
                current = self.verify_amd_install_refresh(current)
            self.verify_amd_install_retry(current, record, transactions[0])
            current = bound_read(AMD_DNF_INSTALL_FROM/'non-target-before.json')
        spec = bound_read(source/'spec.json')
        command = bound_read(source/'command-result.json')
        receipt = bound_read(source/'receipt.json')
        stopped = bound_read(source/'cleanup.json')
        stopped_state = stopped['state']
        after = bound_read(source/'amd-guest-memory-after.json')
        marker = bound_read(self.base/AMD_DNF_RETRY_MARKER)
        if (validate_spec(spec) != self.fixture or spec.get('guest_profile') != AMD_DNF_PROFILE
                or spec.get('unit') == self.spec['unit'] or spec.get('acl_guard_unit') == self.spec['acl_guard_unit']
                or marker.get('attempt') != str(source) or marker.get('binding') != AMD_DNF_BINDING
                or not (AMD_DNF_ABORT_FROM/'abort.json').is_file()
                or marker.get('original_abort_sha256')
                    != hashlib.sha256(bound_bytes(AMD_DNF_ABORT_FROM/'abort.json')).hexdigest()
                or command.get('exit_code') != 0 or receipt.get('reason') is not None
                or receipt.get('command_result') != command or receipt.get('cleanup') != stopped
                or stopped.get('status') != 'STOPPED' or stopped.get('cid') != AMD_BOOTSTRAP_CID
                or stopped_state.get('Running') is not False or stopped_state.get('Pid') != 0
                or stopped['worker_final'].get('MainPID') != '0'
                or stopped['worker_final'].get('ControlGroup') != ''
                or after.get('cid') != AMD_BOOTSTRAP_CID or after.get('transient_scope_gone') is not True
                or after.get('saved_memory') != 384*MIB or after.get('saved_memory_swap') != 512*MIB
                or after['state'].get('StartedAt') != stopped_state.get('StartedAt')
                or after['state'].get('FinishedAt') != stopped_state.get('FinishedAt')
                or current != bound_read(source/'native-after.json') or AMD_BOOTSTRAP_CID not in current
                or any(current[AMD_BOOTSTRAP_CID]['state'].get(key) != stopped_state.get(key)
                       for key in ('StartedAt', 'FinishedAt', 'Running', 'Pid', 'Status'))):
            raise RuntimeError('AMD install does not match the successful stopped query')
        raw = bound_bytes(source/'result.json')
        query = json.loads(raw)
        if (query.get('cid') != AMD_BOOTSTRAP_CID or query.get('status') != 'PASS'
                or query.get('mode') != 'query-continuation' or query.get('formal_stop') != 'STOPPED'
                or query.get('snapshot_policy') != 'archived-offline-query-only'
                or query.get('install_eligible') is not False
                or query['plan'].get('package_plan') != AMD_DNF_EXACT_PLAN
                or len(query['plan'].get('closure', [])) != 28):
            raise RuntimeError('AMD query plan does not match the fixed installation closure')
        digest = hashlib.sha256(raw).hexdigest()
        if digest != AMD_DNF_QUERY_SHA256:
            raise RuntimeError('AMD successful query result differs from the reviewed bytes')
        return query, digest

    def verify_amd_install_inputs(self, query, digest, current):
        profile = self.spec.get('guest_profile')
        command, controller_sha = amd_install_controller(profile)
        controller = Path(command[2])
        for directory in (controller.parent, AMD_DNF_PRIMARY.parent, AMD_DNF_PREFETCH.parent,
                          AMD_DNF_METALINK.parent, AMD_DNF_PAYLOAD):
            owned_directory(directory, private=True)
        if hashlib.sha256(bound_bytes(controller)).hexdigest() != controller_sha:
            raise RuntimeError('AMD offline installer differs from the reviewed controller')
        primary_raw = bound_bytes(AMD_DNF_PRIMARY)
        prefetch_raw = bound_bytes(AMD_DNF_PREFETCH)
        metalink_raw = bound_bytes(AMD_DNF_METALINK)
        verify_amd_host_prefetch_exit(AMD_DNF_PREFETCH.parent, 0, current)
        verify_amd_host_prefetch_exit(AMD_DNF_PREFETCH_ORIGINAL, 1, current)
        now = time.time()
        historical_at = None
        if profile == AMD_DNF_INSTALL_REFRESH_PROFILE:
            historical_at = bound_read(AMD_DNF_PREFETCH.parent/'command-result.json')['at']
        proof = validate_amd_package_receipts(
            query, digest, primary_raw, prefetch_raw, metalink_raw, now, historical_at=historical_at)
        verify_amd_prefetch_continuation(json.loads(prefetch_raw))
        verify_amd_fresh_metalinks(json.loads(metalink_raw), proof, self.fixture.shared,
                                  now if historical_at is None else historical_at)
        for row in proof['packages']:
            path = Path(row['file'])
            owned_directory(path.parent, private=True)
            file = path.lstat()
            if (not stat.S_ISREG(file.st_mode) or file.st_uid != self.fixture.uid
                    or file.st_mode & 0o022 or file.st_size != row['download_bytes']):
                raise RuntimeError('AMD prefetched RPM file type, owner or size differs')
        return proof

    def verify_amd_install_refresh(self, current):
        source = AMD_DNF_INSTALL_RETRY_FROM
        owned_directory(source, private=True)
        for name in ('abort.json', 'amd-offline-install-ready.json', 'package-transaction.json',
                     'admitted-package-step-0.json'):
            absent(source/name)
        receipts = {}
        for name, digest in AMD_DNF_INSTALL_RETRY_FAILURE_HASHES.items():
            raw = bound_bytes(source/name)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise RuntimeError('AMD no-submission attempt evidence changed: '+name)
            if name.endswith('.json'):
                receipts[name] = json.loads(raw)
        result, spec = receipts['result.json'], receipts['spec.json']
        command, receipt, cleanup = (receipts[name] for name in
                                     ('command-result.json', 'receipt.json', 'cleanup.json'))
        previous = receipts['non-target-before.json']
        memory_after = receipts['amd-guest-memory-after.json']
        before = receipts['amd-offline-install-before.json']
        marker = bound_read(self.base/AMD_DNF_INSTALL_RETRY_MARKER)
        phases = [(row['label'], row['exit_code']) for row in result['commands']]
        expected_phases = [
            ('cache-allocated-before', 0), ('dnf-main', 0), ('dnf-repos', 0),
            ('import-tools', 0), ('rpm-trusted-key', 0), ('verify-cached-rpms', 0),
            ('rpm-signatures', 0), ('dnf-repos', 0), ('dnf-solver', 1),
            ('rpm-inventory', 0), ('dnf-package-sizes', 0),
        ]
        if (result.get('status') != 'FAIL' or result.get('formal_stop') != 'STOPPED'
                or result.get('recovery_required') is not False or result.get('cid') != AMD_BOOTSTRAP_CID
                or 'transaction' in result or result.get('error') != {
                    'type': 'RuntimeError',
                    'detail': 'insufficient remaining AMD phase or snapshot time for formal installation'}
                or result.get('plan') != bound_read(AMD_DNF_SUCCEEDED_QUERY/'result.json')['plan']
                or phases != expected_phases or any('--setopt=gpgcheck=1' not in result['commands'][i]['argv']
                                                   for i in (1, 2, 7, 8, 10))
                or spec.get('guest_profile') != AMD_DNF_INSTALL_RETRY_PROFILE
                or spec.get('unit') == self.spec['unit'] or spec.get('acl_guard_unit') == self.spec['acl_guard_unit']
                or marker.get('attempt') != str(source) or marker.get('binding') != AMD_DNF_BINDING
                or marker.get('prefetch_sha256') != before.get('prefetch_sha256')
                or command.get('exit_code') != 1 or receipt.get('reason') is not None
                or not 0 < before['valid_until_unix']-command['at'] < 150
                or receipt.get('command_result') != command or receipt.get('cleanup') != cleanup
                or receipts['rescue.json'] != cleanup or cleanup.get('status') != 'STOPPED'
                or cleanup.get('cid') != AMD_BOOTSTRAP_CID or cleanup['worker_final'].get('MainPID') != '0'
                or cleanup['worker_final'].get('ControlGroup') != ''
                or memory_after.get('cid') != AMD_BOOTSTRAP_CID or memory_after.get('transient_scope_gone') is not True
                or memory_after.get('saved_memory') != 384*MIB or memory_after.get('saved_memory_swap') != 512*MIB
                or current != receipts['native-after.json'] or current.keys() != previous.keys()
                or any(current[key] != previous[key] for key in current if key != AMD_BOOTSTRAP_CID)
                or any(current[AMD_BOOTSTRAP_CID]['state'][key] != cleanup['state'][key]
                       or cleanup['state'][key] != memory_after['state'][key]
                       for key in ('StartedAt', 'FinishedAt', 'Status', 'Running', 'Pid'))):
            raise RuntimeError('AMD refresh requires the stopped retry that reached the pre-submit time gate only')
        return previous

    def verify_amd_refreshed_inputs(self, history, current):
        verify_amd_host_prefetch_exit(AMD_DNF_REFRESH_FROM, 0, current)
        owned_directory(AMD_DNF_REFRESH_METALINK.parent, private=True)
        raw = bound_bytes(AMD_DNF_REFRESH_FROM/'result.json')
        metalink_raw = bound_bytes(AMD_DNF_REFRESH_METALINK)
        report, metalink = json.loads(raw), json.loads(metalink_raw)
        now = time.time()
        old_packages = bound_read(AMD_DNF_PREFETCH)['packages']
        cutoff = bound_read(AMD_DNF_INSTALL_RETRY_FROM/'command-result.json')['at']
        expiry = validate_amd_refresh_receipt(report, metalink, metalink_raw, history, old_packages, cutoff, now)
        verify_amd_fresh_metalinks(metalink, history, self.fixture.shared, now,
                                  source=AMD_DNF_REFRESH_METALINK.parent, ttl=1200,
                                  allow_primary_updates=True)
        observed_headers = 0
        for repo, row in metalink['repos'].items():
            transport = bound_read(AMD_DNF_REFRESH_METALINK.parent/(repo+'.curl.json'))
            keys = ('url', 'url_effective', 'http_code', 'ssl_verify_result', 'num_redirects',
                    'exitcode', 'size_download')
            size = transport.get('size_header')
            if (any(transport.get(key) != row.get(key) for key in keys)
                    or type(size) is not int or not 0 < size <= 512*1024):
                raise RuntimeError('AMD refresh raw official HTTPS transport evidence differs: '+repo)
            observed_headers += size
        if observed_headers != report['budget']['metadata_refresh_response_header_bytes']:
            raise RuntimeError('AMD refresh official HTTP header accounting differs')
        return {**history, 'historical_valid_until_unix': history['valid_until_unix'],
                'valid_until_unix': expiry, 'refresh_sha256': hashlib.sha256(raw).hexdigest(),
                'refreshed_metalink_sha256': report['metalink_receipt_sha256'], 'budget': report['budget']}

    def verify_amd_install_retry(self, current, record, transaction):
        source = AMD_DNF_INSTALL_FROM
        owned_directory(source, private=True)
        absent(source/'abort.json')
        proof = {}
        for name, digest in AMD_DNF_INSTALL_FAILURE_HASHES.items():
            raw = bound_bytes(source/name)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise RuntimeError('AMD initial install evidence changed: '+name)
            if name.endswith('.json'):
                proof[name] = json.loads(raw)
        old_ready = proof['amd-offline-install-ready.json']
        old_before = proof['amd-offline-install-before.json']
        marker = bound_read(self.base/AMD_DNF_INSTALL_MARKER)
        receipt, cleanup = proof['receipt.json'], proof['cleanup.json']
        command, old_spec = proof['command-result.json'], proof['spec.json']
        after = proof['amd-guest-memory-after.json']
        stopped = cleanup['state']
        before = bound_read(source/'non-target-before.json')
        original = proof['package-transaction.json']
        inventory = bound_bytes(EnvironmentStore(self.fixture.root).inventory_path(self.fixture.env_id))
        if (transaction != original or transaction['environment_binding'] != AMD_DNF_BINDING
                or transaction['revision_before'] != transaction['revision_after']
                or transaction['revision_before']['package_inventory_sha256'] != AMD_DNF_INSTALL_UNCHANGED_INVENTORY
                or record.get('runtime_state') != 'stopped' or record.get('package_inventory_status') != 'verified'
                or record.get('content_revision') != transaction['revision_before']['content_revision']
                or record.get('writable_revision') != transaction['revision_before']['writable_revision']
                or record.get('package_inventory_count') != 147 or len(inventory.splitlines()) != 147
                or record.get('package_inventory_sha256') != AMD_DNF_INSTALL_UNCHANGED_INVENTORY
                or hashlib.sha256(inventory).hexdigest() != AMD_DNF_INSTALL_UNCHANGED_INVENTORY
                or marker.get('attempt') != str(source) or marker.get('binding') != AMD_DNF_BINDING
                or marker.get('prefetch_sha256') != old_ready.get('prefetch_sha256')
                or old_ready.get('prefetch_sha256') != old_before.get('prefetch_sha256')
                or old_ready.get('verified_rpms') != old_before.get('packages')
                or old_ready.get('signing_fingerprint') != AMD_DNF_SIGNING_FINGERPRINT
                or old_spec.get('guest_profile') != AMD_DNF_INSTALL_PROFILE
                or old_spec.get('unit') == self.spec['unit'] or old_spec.get('acl_guard_unit') == self.spec['acl_guard_unit']
                or command.get('exit_code') != 1 or receipt.get('reason') is not None
                or receipt.get('command_result') != command or receipt.get('cleanup') != cleanup
                or proof['rescue.json'] != cleanup
                or cleanup.get('status') != 'STOPPED' or cleanup.get('cid') != AMD_BOOTSTRAP_CID
                or stopped.get('Running') is not False or stopped.get('Pid') != 0
                or cleanup['worker_final'].get('MainPID') != '0'
                or after['cid'] != AMD_BOOTSTRAP_CID or after['transient_scope_gone'] is not True
                or after['saved_memory'] != 384*MIB or after['saved_memory_swap'] != 512*MIB
                or current != proof['native-after.json'] or current.keys() != before.keys()
                or any(current[cid] != before[cid] for cid in current if cid != AMD_BOOTSTRAP_CID)
                or any(current[AMD_BOOTSTRAP_CID]['state'][key] != stopped[key]
                       or stopped[key] != after['state'][key]
                       for key in ('StartedAt', 'FinishedAt', 'Running', 'Pid', 'Status'))):
            raise RuntimeError('AMD retry requires the exact stopped DNF option-parser failure and unchanged inventory')
        validate_amd_dnf_option_failure(transaction)

    def claim_amd_offline_install(self):
        if self.check_preconditions() != bound_read(self.out/'non-target-before.json'):
            raise RuntimeError('AMD native inventory changed before the one permitted installation')
        proof = bound_read(self.out/'amd-offline-install-before.json')
        claim = {'attempt': str(self.out), 'binding': AMD_DNF_BINDING,
                 'prefetch_sha256': proof['prefetch_sha256'], 'owner': reuse.process(os.getpid())}
        if self.spec.get('guest_profile') == AMD_DNF_INSTALL_REFRESH_PROFILE:
            claim['refresh_sha256'] = proof['refresh_sha256']
        register_intent(self.base/amd_install_marker(self.spec.get('guest_profile')), claim)

    def package_maintenance_marker(self):
        name = ('package-recovery-attempt.json' if self.phase == 'recover'
                else 'package-native-repair-attempt.json')
        return self.base/name

    def verify_recovery_provenance(self, current):
        previous = INTEL_RECOVER_FROM
        owned_directory(previous, private=True)
        absent(self.package_maintenance_marker())
        old_spec = bound_read(previous/'spec.json')
        abort = bound_read(previous/'abort.json')
        receipt = bound_read(previous/'receipt.json')
        clean = bound_read(previous/'cleanup.json')
        live = bound_read(previous/'live-binding.json')
        earlier = bound_read(previous/'non-target-before.json')
        after = bound_read(previous/'native-after.json')
        stopped = clean.get('state', {})
        worker = clean.get('worker_final', {})
        cid = INTEL_RECOVER_BINDING['container_id']
        if (validate_spec(old_spec) != self.fixture or old_spec['phase'] != 'prepare'
                or abort.get('reason') != 'INVALID_HARD_LIMIT' or receipt.get('reason') != abort['reason']
                or receipt.get('cleanup') != clean or bound_read(previous/'rescue.json') != clean
                or clean.get('status') != 'STOPPED' or clean.get('cid') != cid
                or stopped.get('Running') is not False or stopped.get('Pid') != 0
                or stopped.get('Status') != 'exited' or stopped.get('OOMKilled') is not False
                or live.get('cid') != cid or live.get('coverage_verified') is not True
                or live.get('started_at') != stopped.get('StartedAt')
                or worker.get('MainPID') != '0' or worker.get('ControlGroup') != ''
                or cid not in after
                or any(after[cid]['state'].get(key) != stopped.get(key)
                       for key in ('StartedAt', 'FinishedAt', 'Running', 'Pid', 'Status'))
                or {key: value for key, value in after.items() if key != cid}
                   != {key: value for key, value in earlier.items() if key != cid}):
            raise RuntimeError('Intel package abort or stopped native inventory differs')
        path = EnvironmentStore(self.fixture.root).transaction_path(INTEL_RECOVER_TRANSACTION)
        raw = bound_bytes(path)
        txn = json.loads(raw)
        record, pending = self.pending_transactions()
        steps = txn.get('native', [])
        install = bound_read(previous/'admitted-package-step-1.json')
        if (binding_for(record) != INTEL_RECOVER_BINDING
                or pending != [txn] or txn.get('id') != INTEL_RECOVER_TRANSACTION
                or txn.get('environment_binding') != INTEL_RECOVER_BINDING
                or txn.get('phase') != 'running' or txn.get('operation') != 'install'
                or len(steps) != 2 or steps[0].get('exit_code') != 0
                or steps[1].get('argv') != install.get('logical_argv')
                or steps[1].get('exit_code') != 137 or install.get('plan') != old_spec.get('package_plan')):
            raise RuntimeError('Intel package recovery requires the exact interrupted transaction')
        plan = self.verify_repair_plan(old_spec['package_plan'])
        proof = {'source': str(previous), 'binding': INTEL_RECOVER_BINDING, 'transaction': txn,
                 'transaction_sha256': hashlib.sha256(raw).hexdigest(),
                 'abort_sha256': hashlib.sha256(bound_bytes(previous/'abort.json')).hexdigest(),
                 'cleanup_sha256': hashlib.sha256(bound_bytes(previous/'cleanup.json')).hexdigest(),
                 'signed_archive_plan_sha256': plan}
        if self.phase == 'recover':
            proof['native_repair'] = self.verify_native_repair(after, current, proof)
        elif current != after:
            raise RuntimeError('container state changed after the Intel package abort')
        return proof

    def verify_repair_plan(self, admitted):
        raw = bound_bytes(INTEL_REPAIR_PLAN)
        result = json.loads(raw)
        plan = result['plan']
        closure = plan['closure']
        repair_cache_entries(closure)
        archives = [row for row in closure if row.get('name') == 'libllvm19']
        expected = {'arch': 'amd64', 'version': '1:19.1.7-3+b1', 'download_bytes': INTEL_REPAIR_SIZE,
                    'sha256': INTEL_REPAIR_SHA256}
        if (result.get('status') != 'PASS' or result.get('cid') != INTEL_RESUME_CID
                or plan.get('package_plan') != admitted or admitted.get('requests') != INTEL_REPAIR_REQUESTS
                or len(archives) != 1
                or any(archives[0].get(key) != value for key, value in expected.items())):
            raise RuntimeError('native repair archive differs from the original signed plan')
        return hashlib.sha256(raw).hexdigest()

    def verify_native_repair(self, original_after, current, provenance):
        previous = INTEL_REPAIR_FROM
        owned_directory(previous, private=True)
        absent(previous/'abort.json')
        spec = bound_read(previous/'spec.json')
        claim = bound_read(self.base/'package-native-repair-attempt.json')
        receipt = bound_read(previous/'receipt.json')
        command = bound_read(previous/'command-result.json')
        cleanup = bound_read(previous/'cleanup.json')
        report = bound_read(previous/'native-maintenance.json')
        admitted = bound_read(previous/'recovery-before.json')
        worker = cleanup.get('worker_final', {})
        stopped = cleanup.get('state', {})
        cid = INTEL_RESUME_CID
        if (validate_spec(spec) != self.fixture or spec['phase'] != 'native-repair'
                or claim.get('attempt') != str(previous) or claim.get('binding') != INTEL_RECOVER_BINDING
                or claim.get('transaction') != INTEL_RECOVER_TRANSACTION
                or claim.get('transaction_sha256') != admitted.get('transaction_sha256')
                or not reuse.same_execution(claim.get('owner'), bound_read(previous/'command-identity.json'))
                or {key: value for key, value in admitted.items() if key not in ('transaction', 'transaction_sha256')}
                   != {key: value for key, value in provenance.items() if key not in ('transaction', 'transaction_sha256')}
                or receipt.get('reason') is not None or receipt.get('command_result') != command
                or receipt.get('cleanup') != cleanup
                or bound_read(previous/'rescue.json') != cleanup
                or cleanup.get('status') != 'STOPPED' or cleanup.get('cid') != cid
                or stopped.get('Running') is not False or stopped.get('Pid') != 0
                or worker.get('MainPID') != '0' or worker.get('ControlGroup') != ''
                or bound_read(previous/'non-target-before.json') != original_after
                or bound_read(previous/'native-after.json') != current or cid not in current
                or any(current[cid]['state'].get(key) != stopped.get(key)
                       for key in ('StartedAt', 'FinishedAt', 'Running', 'Pid', 'Status'))
                or {key: value for key, value in current.items() if key != cid}
                   != {key: value for key, value in original_after.items() if key != cid}):
            raise RuntimeError('required native repair did not cleanly stop the pinned Intel fixture')
        archive = report.get('verified_archive', {})
        if (report.get('kind') != 'native-maintenance-only'
                or report.get('binding') != INTEL_RECOVER_BINDING
                or report.get('transaction_id') != INTEL_RECOVER_TRANSACTION
                or report.get('package_recovery_completed') is not False
                or report.get('stopped') != {'cid': cid, 'pid': 0}
                or report.get('transaction_before') != admitted.get('transaction')
                or report.get('transaction_after') != provenance['transaction']
                or not same_pending_transaction_after_stop(admitted['transaction'], provenance['transaction'])
                or archive.get('path') != INTEL_REPAIR_ARCHIVE or archive.get('size') != INTEL_REPAIR_SIZE
                or archive.get('sha256') != INTEL_REPAIR_SHA256):
            raise RuntimeError('native repair receipt did not verify the original package plan')
        closure = bound_read(INTEL_REPAIR_PLAN)['plan']['closure']
        verify_repair_cache_report(report, repair_cache_entries(closure))
        actions = [step for step in report.get('commands', []) if step.get('native_package_exec') is True]
        if (len(actions) != len(INTEL_REPAIR_STEPS)
                or any(action.get('label') != label or action.get('argv') != argv
                       or action.get('state') != 'finished' or action.get('exit_code') != 0
                       or action.get('environment') != intel_repair_environment()
                       for (label, argv), action in zip(INTEL_REPAIR_STEPS.items(), actions))):
            raise RuntimeError('native repair commands did not complete the reviewed transaction repair')
        audit = actions[-1]
        audit_log = audit.get('stdout', '')
        if (not re.fullmatch(r'maintenance-[0-9]{2}-audit\.stdout', audit_log)
                or audit.get('stdout_bytes') != 0 or bound_bytes(previous/audit_log)):
            raise RuntimeError('native repair dpkg audit is not empty')
        result = {'attempt': str(previous), 'audit_stdout': audit_log, 'cid': cid,
                  'maintenance_sha256': hashlib.sha256(bound_bytes(previous/'native-maintenance.json')).hexdigest()}
        if (command.get('exit_code') == 0
                and report.get('status') == 'NATIVE_REPAIRED_REQUIRES_PKG_RECOVER'
                and report.get('completed_original_closure') == 53):
            return result
        if (command.get('exit_code') != 1 or report.get('status') != 'FAILED'
                or report.get('error') != 'native repair changed packages outside the reviewed set or remains incomplete'):
            raise RuntimeError('native repair did not complete and is outside the exact reconciliation')
        result['readonly_reconciliation'] = self.verify_intel_reconciliation(previous, report)
        return result

    def verify_intel_reconciliation(self, previous, report):
        raw = bound_bytes(INTEL_RECONCILIATION)
        value = json.loads(raw)
        after_log = previous/'maintenance-07-inventory-after.stdout'
        if (value.get('schema_version') != 1 or value.get('kind') != 'readonly-package-reconciliation'
                or value.get('status') != 'RECONCILED_REQUIRES_PKG_RECOVER'
                or value.get('source_attempt') != str(previous)
                or value.get('binding') != INTEL_RECOVER_BINDING
                or value.get('transaction_id') != INTEL_RECOVER_TRANSACTION
                or value.get('package_recovery_completed') is not False
                or value.get('original_exact_requests') != INTEL_REPAIR_REQUESTS
                or value.get('verified_closure') != report.get('verified_closure')
                or value.get('transaction_after') != report.get('transaction_after')
                or value.get('installed_original_closure') != 52
                or value.get('omitted_recommendation') != INTEL_OPTIONAL_RECOMMENDATION
                or value.get('inventory_after_sha256') != hashlib.sha256(bound_bytes(after_log)).hexdigest()):
            raise RuntimeError('read-only package reconciliation does not match the recorded optional omission')
        self.verify_reconciliation_sources(previous, value.get('source_files'))
        return {'path': str(INTEL_RECONCILIATION), 'sha256': hashlib.sha256(raw).hexdigest(),
                'installed_original_closure': 52, 'omitted_recommendation': INTEL_OPTIONAL_RECOMMENDATION}

    def verify_reconciliation_sources(self, previous, rows):
        expected = {str(path): path for path in intel_reconciliation_sources(previous)}
        if not isinstance(rows, list) or len(rows) != len(expected):
            raise RuntimeError('read-only package reconciliation source set differs')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('path'), str):
                raise RuntimeError('read-only package reconciliation contains an unbound source')
            path = expected.pop(row['path'], None)
            if path is None:
                raise RuntimeError('read-only package reconciliation contains an unbound source')
            payload = bound_bytes(path)
            if (type(row.get('bytes')) is not int or row['bytes'] != len(payload)
                    or row.get('sha256') != hashlib.sha256(payload).hexdigest()):
                raise RuntimeError('read-only package reconciliation source fingerprint mismatch')

    def verify_recovery_claim(self):
        proof = bound_read(self.out/'recovery-before.json')
        claim = bound_read(self.package_maintenance_marker())
        path = EnvironmentStore(self.fixture.root).transaction_path(INTEL_RECOVER_TRANSACTION)
        if (claim.get('attempt') != str(self.out) or claim.get('binding') != INTEL_RECOVER_BINDING
                or claim.get('transaction_sha256') != proof.get('transaction_sha256')
                or claim.get('transaction') != INTEL_RECOVER_TRANSACTION
                or not reuse.same_execution(claim.get('owner'), reuse.process(os.getpid()))
                or hashlib.sha256(bound_bytes(path)).hexdigest() != claim['transaction_sha256']):
            raise RuntimeError('Intel package recovery worker or original transaction changed')

    def check_preconditions(self):
        if (self.base/'.guard-abort.json').exists():
            raise RuntimeError('persistent fixture abort requires explicit reconciliation')
        state = snapshot()
        fixture = self.fixture
        self.verify_cached_image()
        if self.phase == 'create':
            absent(self.binding_path)
            absent(self.once)
            if 'retry_setup_from' in self.spec:
                self.verify_cuda_setup_retry(state)
            else:
                for path in (fixture.root, fixture.shared, self.hooks):
                    absent(path)
            if any(row['name'] == fixture.name or row['labels'].get('multilinux.project') == str(fixture.root)
                   for row in state.values()):
                raise RuntimeError('approved name or project already owns a container')
        else:
            config = self.active_config()
            owned_directory(fixture.root, private=True)
            owned_directory(fixture.shared, private=True)
            owned_directory(self.hooks, private=True)
            if next(self.hooks.iterdir(), None) is not None:
                raise RuntimeError('private empty OCI hooks directory is no longer empty')
            if self.phase == 'resume':
                proof = self.verify_resume_provenance(state)
                path = self.out/'resume-proof.json'
                if path.exists():
                    if proof != bound_read(path):
                        raise RuntimeError('Intel recovery evidence changed after admission')
                else:
                    reuse.put(path, proof)
            if self.phase == 'amd-bootstrap-recover':
                proof = self.verify_amd_bootstrap_provenance(state)
                path = self.out/'amd-bootstrap-before.json'
                if path.exists():
                    if proof != bound_read(path):
                        raise RuntimeError('AMD bootstrap provenance changed after admission')
                else:
                    reuse.put(path, proof)
            if self.phase in PACKAGE_MAINTENANCE_PHASES:
                if self.phase == 'native-repair' and (
                        self.out != INTEL_REPAIR_FROM
                        or hashlib.sha256(bound_bytes(INTEL_REPAIR_CONTROLLER)).hexdigest()
                            != INTEL_REPAIR_CONTROLLER_SHA256):
                    raise RuntimeError('native repair requires its reviewed controller and receipt directory')
                proof = self.verify_recovery_provenance(state)
                path = self.out/'recovery-before.json'
                if path.exists():
                    if proof != bound_read(path):
                        raise RuntimeError('Intel package recovery evidence changed after admission')
                else:
                    reuse.put(path, proof)
            elif self.phase == 'media-recover':
                self.media_recovery_proof()
            elif self.phase in {'prepare', 'gpu', 'vulkan', 'media'}:
                if self.spec.get('qq_recovery'):
                    service = self.service()
                    pending = service._pending_package_transactions(fixture.env_id)
                    if ([txn['id'] for txn in pending] != [self.spec['qq_recovery']]
                            or pending[0]['environment_binding'] != INTEL_RECOVER_BINDING
                            or pending[0].get('phase') not in ('needs-recovery', 'running')
                            or pending[0].get('packages') != ['linuxqq']):
                        raise RuntimeError('QQ recovery transaction changed or other recovery is pending')
                else:
                    self.require_clean_packages()
                if 'arch_plan_sha256' in self.spec:
                    self.arch_package_plan()
            if self.spec.get('guest_profile') == AMD_DNF_PROFILE:
                proof = self.verify_amd_dnf_retry(state)
                path = self.out/'amd-dnf-retry-before.json'
                if path.exists():
                    if proof != bound_read(path):
                        raise RuntimeError('AMD DNF retry evidence changed after admission')
                else:
                    reuse.put(path, proof)
            if self.spec.get('guest_profile') in AMD_DNF_INSTALL_PROFILES:
                query, digest = self.verify_amd_install_query(state)
                prefetch_state = bound_read(AMD_DNF_SUCCEEDED_QUERY/'native-after.json')
                proof = self.verify_amd_install_inputs(query, digest, prefetch_state)
                if (self.spec.get('guest_profile') != AMD_DNF_INSTALL_PROFILE
                        and proof != bound_read(AMD_DNF_INSTALL_FROM/'amd-offline-install-before.json')):
                    raise RuntimeError('AMD retry payload or fresh authority differs from the first install')
                if self.spec.get('guest_profile') == AMD_DNF_INSTALL_REFRESH_PROFILE:
                    proof = self.verify_amd_refreshed_inputs(proof, state)
                path = self.out/'amd-offline-install-before.json'
                if path.exists():
                    if proof != bound_read(path):
                        raise RuntimeError('AMD offline installation evidence changed after admission')
                else:
                    reuse.put(path, proof)
            observed = reuse.info(config, hardware=self)
            if observed['State']['Running'] or observed['State']['Pid']:
                raise RuntimeError('fixture must start each phase stopped')
            if self.phase in ARCH_MAINTENANCE_PHASES:
                self.verify_arch_maintenance(observed['State'])
            if (self.phase == 'resume' and 'retry_from' in self.spec
                    and observed['State'].get('Error') != INTEL_RETRY_START_ERROR):
                raise RuntimeError('Intel created state no longer has the recorded conmon failure')
            path = fixture.root/'.lab/leases'/(fixture.env_id+'.json')
            if os.path.lexists(path):
                leases = bound_read(path)
                if leases.get('leases') or leases.get('bridge'):
                    raise RuntimeError('fixture has leases or a bridge requiring formal recovery')
        return state

    def media_recovery_proof(self):
        previous_stage = self.spec.get('previous_stage', 'install')
        original = self.out.parent/previous_stage
        owned_directory(original, private=True)
        absent(original/'admitted-package-step-1.json')
        previous = bound_read(original/'receipt.json')
        simulation = bound_read(original/'apt-final-simulation.json')
        txn = bound_read(original/'package-transaction.json')
        config = self.active_config()
        record = bound_read(self.record_path)
        pending = [item for item in EnvironmentStore(self.fixture.root).transactions_for(self.fixture.env_id)
                   if item.get('recovery_required')]
        if len(pending) != 1 or len(txn['native']) != 2:
            raise RuntimeError('Intel media recovery needs one interrupted install transaction')
        current = pending[0]
        stopped = {**txn, 'container_stopped_at': current.get('container_stopped_at'),
                   'native': [txn['native'][0], {**txn['native'][1], 'cleanup': 'container-stopped'}]}
        diagnostic = ('Unable to fetch some archives' if previous_stage == 'install'
                      else 'E: Unsupported file ')
        if (not bound_read(original/'spec.json').get('intel_media_cache')
                or previous['cleanup']['status'] != 'STOPPED' or previous['reason'] is not None
                or simulation['exit_code'] != 100 or diagnostic not in simulation['stderr']
                or current != stopped or not current.get('container_stopped_at') or not txn.get('recovery_required')
                or txn['environment_binding'] != binding_for(record)
                or txn['environment_binding']['container_id'] != config['cid']
                or txn['revision_before'] != txn['revision_after']
                or txn['revision_before']['package_inventory_sha256'] != record['package_inventory_sha256']
                or txn['native'][0].get('exit_code') != 0
                or txn['native'][1].get('guest') is not None or txn['native'][1].get('exit_code') is not None):
            raise RuntimeError('Intel media recovery requires the exact failed pre-submission cache check')
        return txn

    def recover_intel_media(self):
        verify_hardware_permission(self)
        txn = self.media_recovery_proof()
        service = self.service()
        result = service.recover_packages(self.fixture.env_id)
        reuse.put(self.out/'result.json', result)
        if (result.get('status') != 'recovered' or result.get('transactions') != [txn['id']]
                or result['package_inventory_sha256'] != txn['revision_before']['package_inventory_sha256']):
            raise RuntimeError('Intel media recovery inventory does not match the pre-write snapshot')

    def verify_cuda_setup_retry(self, current):
        previous = V100_A_SETUP_ATTEMPT
        receipt = bound_read(previous/'receipt.json')
        setup = bound_read(self.base/'create-attempt.json')
        for name in ('create-intent.json', 'created-container.json', 'start-intent.json', 'abort.json'):
            absent(previous/name)
        if (setup.get('attempt') != str(previous) or setup.get('fixture') != self.spec['fixture']
                or receipt['cleanup']['status'] != 'NO_START'
                or receipt['cleanup']['worker_final']['MainPID'] != '0'
                or receipt['cleanup']['worker_final']['ControlGroup'] != ''
                or receipt['reason'] is not None or receipt['command_result']['exit_code'] != 2
                or bound_bytes(previous/'command.log').strip() != b"No module named 'multilinux'"
                or bound_bytes(previous/'events.jsonl').strip()
                or current != bound_read(previous/'native-after.json')):
            raise RuntimeError('V100 pre-submission failure does not match the retained setup evidence')
        for path in (self.fixture.root, self.fixture.shared, self.hooks):
            owned_directory(path, private=True)
            if next(path.iterdir(), None) is not None:
                raise RuntimeError('V100 setup retry requires the original empty project directories')
        from multilinux.service import EnvironmentService
        from unifier.service import EnvironmentService as OfficialService
        if EnvironmentService is not OfficialService:
            raise RuntimeError('V100 source snapshot is still incomplete')

    def begin_create(self):
        reuse.verify_permission(self.config, self.out)
        state = self.check_preconditions()
        if state != bound_read(self.out/'non-target-before.json'):
            raise RuntimeError('native inventory changed between admission and create')
        register_intent(self.once, {'attempt': str(self.out), 'fixture': self.spec['fixture'],
                                    'owner': reuse.process(os.getpid())})
        if 'retry_setup_from' not in self.spec:
            for path in (self.fixture.root, self.fixture.shared, self.hooks):
                path.mkdir(mode=0o700)
        self.create_service()

    def begin_resume(self):
        verify_hardware_permission(self)
        if self.check_preconditions() != bound_read(self.out/'non-target-before.json'):
            raise RuntimeError('native inventory changed between admission and Intel recovery')
        register_intent(self.resume_marker(),
                        {'attempt': str(self.out), 'original_attempt': str(INTEL_RESUME_FROM),
                         'cid': INTEL_RESUME_CID, 'owner': reuse.process(os.getpid())})
        fixture = self.fixture
        record = self.service().resume_create(fixture.env_id, fixture.env_id, [str(fixture.shared)],
                                              expected_container_id=INTEL_RESUME_CID,
                                              network='pasta', devices=[NODE])
        self.verify_official_binding(record)

    def begin_amd_bootstrap_recover(self):
        verify_hardware_permission(self)
        service = self.service()
        fixture = self.fixture
        with service._creation_lock():
            if self.check_preconditions() != bound_read(self.out/'non-target-before.json'):
                raise RuntimeError('AMD stopped instance changed before formal bootstrap completion')
            register_intent(self.base/AMD_BOOTSTRAP_MARKER,
                            {'attempt': str(self.out), 'original_attempt': str(AMD_BOOTSTRAP_FROM),
                             'cid': AMD_BOOTSTRAP_CID, 'owner': reuse.process(os.getpid())})
            spec, target, relationship, session = service._prepare_create_locked(
                fixture.env_id, fixture.env_id, [str(fixture.shared)], 'pasta', False,
                None, 'rootless-default', [NODE], False, False, 'private', 'preserve-host')
            if spec != fixture_create_spec(fixture) or session is not None:
                raise RuntimeError('formal service parameter binding differs for AMD bootstrap')
            record = service._finish_create(spec, fixture.env_id, target, relationship, None,
                                            expected_container_id=AMD_BOOTSTRAP_CID)
        self.verify_official_binding(record)

    def begin_recover(self):
        self.begin_package_maintenance()
        store = EnvironmentStore(self.fixture.root)
        path = store.transaction_path(INTEL_RECOVER_TRANSACTION)
        try:
            result = self.service().recover_packages(self.fixture.env_id)
        finally:
            reuse.put(self.out/'recovery-transaction-after.json', bound_read(path))
        record, pending = self.pending_transactions()
        txn = bound_read(path)
        storage = bound_read(self.storage_observation_path())
        if (result.get('status') != 'recovered' or result.get('transactions') != [INTEL_RECOVER_TRANSACTION]
                or binding_for(record) != INTEL_RECOVER_BINDING or pending
                or record.get('runtime_state') != 'stopped' or record.get('package_recovery_required') is not False
                or record.get('package_inventory_status') != 'verified'
                or result.get('package_inventory_sha256') != record.get('package_inventory_sha256')
                or txn.get('phase') != 'failed' or txn.get('recovery_required') is not False
                or storage.get('state') != 'released' or storage.get('environment_binding') != INTEL_RECOVER_BINDING
                or reuse.info(self.active_config(), hardware=self)['State']['Running']):
            raise RuntimeError('Intel recovery lacks a verified stopped inventory or terminal transaction')
        reuse.put(self.out/'package-recovery-result.json', result)

    def begin_package_maintenance(self):
        verify_hardware_permission(self)
        if self.check_preconditions() != bound_read(self.out/'non-target-before.json'):
            raise RuntimeError('native inventory changed between admission and package maintenance')
        proof = bound_read(self.out/'recovery-before.json')
        register_intent(self.package_maintenance_marker(),
                        {'attempt': str(self.out), 'transaction': INTEL_RECOVER_TRANSACTION,
                         'binding': INTEL_RECOVER_BINDING, 'transaction_sha256': proof['transaction_sha256'],
                         'owner': reuse.process(os.getpid())})
    def storage_observation_path(self):
        cid = self.active_config()['cid'] if self.fixture.env_id == 'arch' else INTEL_RESUME_CID
        return self.fixture.root/'.lab/state/inventory-observations'/(cid+'.json')

    def repair_cache(self):
        proof = bound_read(self.out/'recovery-before.json')
        raw = bound_bytes(INTEL_REPAIR_PLAN)
        if hashlib.sha256(raw).hexdigest() != proof.get('signed_archive_plan_sha256'):
            raise RuntimeError('signed repair cache plan changed after admission')
        return repair_cache_entries(json.loads(raw)['plan']['closure'])

    def set_package_memory_high(self, binding):
        arch = self.fixture.env_id == 'arch' and (
            self.phase in ('replay', 'replay-llvm')
            or (self.phase == 'prepare' and 'arch_plan_sha256' in self.spec))
        if not arch and (self.fixture.driver != 'i915' or not (
                self.phase in PACKAGE_MAINTENANCE_PHASES or 'package_plan' in self.spec)):
            return
        verify_hardware_permission(self)
        init = binding['init']
        if (binding['cid'] != (self.active_config()['cid'] if arch else INTEL_RESUME_CID)
                or not binding.get('coverage_verified')
                or binding['guest_limits'].get('memory.max') != 384*MIB
                or not reuse.same_execution(init, reuse.process(init['pid']))):
            raise RuntimeError('package reclaim requires the registered execution and hard limit')
        high = reuse.cgroup_dir(init['cgroup'])/'memory.high'
        requested = (288 if arch else 256)*MIB
        fd = os.open(high, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            previous = os.read(fd, 64).decode().strip()
            if previous != 'max' and (not previous.isdecimal() or int(previous) < requested):
                raise RuntimeError('package reclaim cannot raise an existing lower watermark')
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(requested).encode())
        finally:
            os.close(fd)
        if high.read_text().strip() != str(requested):
            raise RuntimeError('Intel package reclaim watermark did not take effect')
        reuse.put(self.out/'guest-watermark.json', {'cid': binding['cid'], 'init': init,
            'old_memory_high': previous, 'memory_high': requested, 'memory_max': 384*MIB})

    def apply_amd_dnf_budget(self, binding):
        verify_hardware_permission(self)
        profile = self.spec.get('guest_profile')
        marker = amd_install_marker(profile) if profile in AMD_DNF_INSTALL_PROFILES else AMD_DNF_RETRY_MARKER
        claim = bound_read(self.base/marker)
        config = self.active_config()
        if (profile not in AMD_DNF_MEMORY_PROFILES or not binding.get('coverage_verified')
                or binding['cid'] != AMD_BOOTSTRAP_CID or claim.get('binding') != AMD_DNF_BINDING
                or claim.get('attempt') != str(self.out)
                or not reuse.same_execution(claim.get('owner'), reuse.process(os.getpid()))):
            raise RuntimeError('AMD DNF budget requires the one registered retry and verified execution')
        init = binding['init']
        scope = binding['scope']
        unit = reuse.unit(scope['Id'])
        group = scope['ControlGroup']
        if (scope['Id'] != 'libpod-'+AMD_BOOTSTRAP_CID+'.scope' or unit.get('Id') != scope['Id']
                or not scope.get('InvocationID') or unit.get('InvocationID') != scope['InvocationID']
                or unit.get('ControlGroup') != group or not reuse.under(init['cgroup'], group)
                or not reuse.same_execution(init, reuse.process(init['pid']))):
            raise RuntimeError('AMD DNF scope or execution identity changed before quota transition')
        aggregate = bound_read(self.out/'ready.json')['aggregate']
        before = reuse.effective_limits(init['cgroup'], aggregate)
        if before != binding['guest_limits']:
            raise RuntimeError('AMD DNF scope limits changed before quota transition')
        after = amd_dnf_effective_limits(before, group)
        path = reuse.cgroup_dir(group)
        high = (path/'memory.high').read_text().strip()
        if high != 'max' and (not high.isdecimal() or int(high) < AMD_DNF_ACTIVE_HIGH):
            raise RuntimeError('AMD DNF scope already has a stricter memory.high')
        transition = {'cid': AMD_BOOTSTRAP_CID, 'init': init, 'scope': scope,
                      'before': before, 'after': after, 'old_memory_high': high,
                      'memory_high': AMD_DNF_ACTIVE_HIGH, 'at_monotonic': time.monotonic()}
        register_intent(self.out/AMD_DNF_LIMIT_INTENT, transition)
        if not reuse.same_execution(init, reuse.process(init['pid'])):
            raise RuntimeError('AMD DNF init changed immediately before setting quota')
        write_scope_limit(path/'memory.max', str(AMD_DNF_SAVED_MEMORY), AMD_DNF_ACTIVE_MEMORY)
        write_scope_limit(path/'memory.high', high, AMD_DNF_ACTIVE_HIGH)
        if (reuse.effective_limits(init['cgroup'], aggregate) != after
                or not reuse.same_execution(init, reuse.process(init['pid']))):
            raise RuntimeError('AMD DNF kernel did not retain the exact approved limits')
        reuse.put(self.out/AMD_DNF_LIMIT_APPLIED, transition)
        current = reuse.info(config, hardware=self)
        if current['State']['StartedAt'] != binding['started_at']:
            raise RuntimeError('AMD DNF saved 384 MiB configuration or execution changed')

    def expected_live_memory(self, binding, *, new_start):
        if new_start or self.spec.get('guest_profile') not in AMD_DNF_MEMORY_PROFILES:
            return self.resources['memory']
        applied = bound_read(self.out/AMD_DNF_LIMIT_APPLIED)
        self.verify_amd_dnf_transition(binding, applied)
        return AMD_DNF_ACTIVE_MEMORY

    def qq_active_limits(self, binding):
        applied = bound_read(self.out/'qq-gui-quota-applied.json')
        self.verify_qq_quota(binding, applied)
        return 1024*MIB, 256

    def verify_qq_quota(self, binding, receipt):
        scope = binding['scope']
        if (binding['cid'] != INTEL_RESUME_CID or receipt.get('cid') != INTEL_RESUME_CID
                or not scope.get('InvocationID')
                or any(scope.get(key) != receipt['scope'].get(key)
                       for key in ('Id', 'ControlGroup', 'InvocationID'))
                or not reuse.same_execution(binding['init'], receipt['init'])):
            raise RuntimeError('QQ GUI quota is outside the exact Intel execution')

    def apply_qq_gui_budget(self, binding):
        verify_hardware_permission(self)
        init = binding['init']
        scope = binding['scope']
        transition = {'cid': binding['cid'], 'init': init, 'scope': scope,
                      'before': binding['guest_limits'], 'at': time.monotonic()}
        self.verify_qq_quota(binding, transition)
        aggregate = bound_read(self.out/'ready.json')['aggregate']
        if (not binding.get('coverage_verified') or scope.get('Id') != 'libpod-'+INTEL_RESUME_CID+'.scope'
                or init['cgroup'] != scope['ControlGroup']+'/container'
                or not reuse.under(scope['ControlGroup'], aggregate)
                or not reuse.same_execution(init, reuse.process(init['pid']))):
            raise RuntimeError('QQ GUI cgroup layout changed')
        register_intent(self.out/'qq-gui-quota-intent.json', transition)
        for group in (scope['ControlGroup'], init['cgroup']):
            path = reuse.cgroup_dir(group)
            write_scope_limit(path/'memory.max', str(384*MIB), 1024*MIB)
            write_scope_limit(path/'pids.max', '128', 256)
        path = reuse.cgroup_dir(init['cgroup'])/'memory.high'
        write_scope_limit(path, 'max', 960*MIB)
        actual = reuse.effective_limits(init['cgroup'], aggregate)
        if (not reuse.same_execution(init, reuse.process(init['pid']))
                or actual['memory.max'] != 1024*MIB or actual['pids.max'] != 256
                or actual['memory.swap.max'] != 128*MIB or actual['cpu.max'] != transition['before']['cpu.max']):
            raise RuntimeError('QQ GUI limits or bound execution differ after adjustment')
        reuse.put(self.out/'qq-gui-quota-applied.json', {**transition, 'after': actual})

    def verify_amd_dnf_transition(self, binding, transition):
        scope = binding['scope']
        if (binding.get('cid') != AMD_BOOTSTRAP_CID or transition.get('cid') != AMD_BOOTSTRAP_CID
                or not reuse.same_execution(binding['init'], transition['init'])
                or any(scope.get(key) != transition['scope'].get(key)
                       for key in ('Id', 'ControlGroup', 'InvocationID'))
                or transition['after'] != amd_dnf_effective_limits(transition['before'], scope['ControlGroup'])
                or transition.get('memory_high') != AMD_DNF_ACTIVE_HIGH):
            raise RuntimeError('AMD DNF quota evidence is not tied to this exact scope execution')

    def verify_live_guest_limits(self, live, current, aggregate, now):
        if self.spec.get('qq_gui'):
            applied = self.out/'qq-gui-quota-applied.json'
            intent = self.out/'qq-gui-quota-intent.json'
            if applied.exists():
                receipt = bound_read(applied)
                self.verify_qq_quota(live, receipt)
                if current != receipt['after']:
                    current = reuse.effective_limits(live['init']['cgroup'], aggregate)
                high = reuse.cgroup_dir(live['init']['cgroup'])/'memory.high'
                if current != receipt['after'] or high.read_text().strip() != str(960*MIB):
                    raise RuntimeError('QQ GUI limits changed after admission')
                return current
            if intent.exists():
                receipt = bound_read(intent)
                self.verify_qq_quota(live, receipt)
                before = receipt['before']
                if (now - receipt['at'] > 2 or current['memory.max'] not in (384*MIB, 1024*MIB)
                        or current['pids.max'] not in (128, 256)
                        or current['memory.swap.max'] != before['memory.swap.max']
                        or current['cpu.max'] != before['cpu.max']):
                    raise RuntimeError('QQ GUI quota update did not converge')
                return current
        if self.spec.get('guest_profile') not in AMD_DNF_MEMORY_PROFILES:
            if current != live['guest_limits']:
                raise RuntimeError('fixture cgroup limits changed')
            return current
        applied = self.out/AMD_DNF_LIMIT_APPLIED
        intent = self.out/AMD_DNF_LIMIT_INTENT
        if applied.exists():
            transition = bound_read(applied)
            self.verify_amd_dnf_transition(live, transition)
            if current != transition['after']:
                current = reuse.effective_limits(live['init']['cgroup'], aggregate)
            path = reuse.cgroup_dir(transition['scope']['ControlGroup'])/'memory.high'
            if current != transition['after'] or path.read_text().strip() != str(AMD_DNF_ACTIVE_HIGH):
                raise RuntimeError('AMD DNF active limits changed')
        elif intent.exists():
            transition = bound_read(intent)
            self.verify_amd_dnf_transition(live, transition)
            if (now - transition['at_monotonic'] > 2
                    or current not in (transition['before'], transition['after'])):
                raise RuntimeError('AMD DNF quota transition failed or expired')
        elif current != live['guest_limits']:
            raise RuntimeError('AMD DNF limits changed without a registered transition')
        return current

    def create_service(self):
        fixture = self.fixture
        service = self.service()
        if fixture.cuda:
            record = service.create(fixture.env_id, fixture.env_id, [], network=fixture.network,
                                    cuda_device=fixture.cuda.uuid)
        else:
            record = service.create(fixture.env_id, fixture.env_id, [str(fixture.shared)],
                                    network=fixture.network, devices=list(fixture.devices))
        self.verify_official_binding(record)

    def verify_official_binding(self, record):
        fixture = self.fixture
        config = self.active_config()
        binding = validate_record(fixture, config['cid'], record)
        if bound_read(self.record_path) != record or reuse.info(config, hardware=self)['State']['Running']:
            raise RuntimeError('formal create did not persist the verified stopped fixture')
        reuse.put(self.out/'official-binding.json', binding)

    def service(self):
        targets, lock, _ = load_project()
        item = lock['targets'][self.fixture.env_id]
        if item['locked_reference'] != self.fixture.reference or item['manifest_digest'] != self.fixture.digest:
            raise RuntimeError('worktree image lock differs from the approved fixture')
        from unifier.service import EnvironmentService
        return EnvironmentService(self.fixture.root, HardwareRuntime(self), targets, lock)

    def active_config(self, *, optional=False):
        if self.phase == 'resume':
            return {**self.config, 'cid': INTEL_RESUME_CID}
        if self.phase == 'amd-bootstrap-recover':
            return {**self.config, 'cid': AMD_BOOTSTRAP_CID}
        path = self.out/'created-container.json' if self.phase == 'create' else self.binding_path
        if optional and not path.exists():
            return self.config
        row = bound_read(path)
        if not CID.fullmatch(row.get('cid', '')):
            raise RuntimeError('fixture binding requires one complete CID')
        return {**self.config, **{key: row[key] for key in ('cid', 'environment_uuid', 'generation') if key in row}}

    def validate_container(self, config, value):
        proof = bound_read(self.out/'image-before.json')
        image_digest = proof['container_digest']
        if (proof['reference'] != self.fixture.reference
                or proof['reference_digest'] != self.fixture.digest
                or proof['config_id'] != self.fixture.image_id
                or self.fixture.registry+'@'+image_digest not in proof['config_id_repo_digests']):
            raise RuntimeError('image admission evidence does not match the approved fixture')
        validate_container(self.fixture, value, cid=config['cid'], image_digest=image_digest)
        if self.phase == 'amd-bootstrap-recover' and not os.path.lexists(self.record_path):
            evidence = bound_read(self.out/'amd-bootstrap-before.json')
            if (evidence.get('cid') != config['cid'] or config['cid'] != AMD_BOOTSTRAP_CID
                    or evidence.get('original_attempt') != str(AMD_BOOTSTRAP_FROM)):
                raise RuntimeError('unregistered AMD bootstrap ownership')
            return
        if self.phase == 'resume' and not os.path.lexists(self.record_path):
            start_proof = bound_read(self.out/'resume-proof.json')
            if (start_proof.get('original_attempt') != str(INTEL_RESUME_FROM)
                    or start_proof.get('cid') != config['cid'] or config['cid'] != INTEL_RESUME_CID):
                raise RuntimeError('unregistered Intel recovery ownership')
            return
        if self.phase == 'create' and not os.path.lexists(self.record_path):
            intent = bound_read(self.once)
            if intent.get('attempt') != str(self.out) or config['cid'] != bound_read(self.out/'created-container.json')['cid']:
                raise RuntimeError('unregistered bootstrap ownership')
            return
        binding = validate_record(self.fixture, config['cid'], bound_read(self.record_path))
        if self.phase not in BOOTSTRAP_PHASES and any(config[key] != value for key, value in binding.items()):
            raise RuntimeError('fixture generation/CID no longer matches its immutable registration')

    def validate_oci(self, spec):
        validate_oci(self.fixture, spec)

    def worker_environment(self, env):
        env = clean_environment(env)
        env.pop('DBUS_SESSION_BUS_ADDRESS', None)
        return env

    def event(self, event, cid, status):
        if status not in CONTAINER_ACTIONS:
            return
        if not CID.fullmatch(cid):
            raise RuntimeError('native event lacks a complete CID')
        bound = self.active_config(optional=True)['cid']
        event_time = amd_event_time_ns(event)
        if status == 'create':
            if (self.phase != 'create' or self.create_event is not None
                    or not (self.out/'create-intent.json').exists()
                    or event.get('Name') != self.fixture.name
                    or not matches_labels(self.fixture, event.get('Attributes'))
                    or not isinstance(event_time, int)
                    or event_time < bound_read(self.out/'create-intent.json')['at_ns']
                    or (bound is not None and cid != bound)):
                raise RuntimeError('unexpected create event: '+cid)
            self.create_event = cid
            reuse.put(self.out/'create-event.json', event)
            return
        if (cid != bound or (self.phase == 'create' and self.create_event != cid)
                or status in {'restart', 'unpause', 'restore', 'remove', 'update'}):
            raise RuntimeError('unexpected native container action: '+cid+':'+status)
        if (not (self.out/'start-intent.json').exists()
                or not isinstance(event_time, int)
                or event_time < bound_read(self.out/'start-intent.json')['at_ns']):
            raise RuntimeError('container action without a guarded start intent: '+status)
        if status == 'start':
            if self.start_seen:
                raise RuntimeError('unregistered second start')
            self.start_seen = True

    def finished(self, pending):
        result = bound_read(self.out/'command-result.json')
        if pending or time.time() - result['at'] < .5:
            return False
        if (self.spec.get('guest_profile') in AMD_DNF_MEMORY_PROFILES and result['exit_code'] == 0
                and (not self.start_seen or not (self.out/AMD_DNF_LIMIT_APPLIED).exists())):
            raise RuntimeError('AMD DNF retry finished without a verified quota transition')
        if self.phase in PACKAGE_MAINTENANCE_PHASES and result['exit_code'] == 0 and not self.start_seen:
            return False
        if self.phase in BOOTSTRAP_PHASES and result['exit_code'] == 0:
            if not self.start_seen:
                return False
            if self.phase == 'create' and self.create_event is None:
                return False
            if self.phase == 'create' and self.create_event != self.active_config()['cid']:
                raise RuntimeError('approved create did not converge on its returned full CID')
        return True

    def check_disk(self, *, initial=False):
        values = {}
        for path in self.sample_roots:
            sv = os.statvfs(path)
            free = sv.f_bavail * sv.f_frsize
            if free < max(5*1024*MIB, math.ceil(sv.f_blocks*sv.f_frsize/10)):
                raise RuntimeError('fixture disk reserve reached: '+str(path))
            values[str(path.stat().st_dev)] = free
        if initial:
            baseline = self.base/'disk-baseline.json'
            if not baseline.exists():
                reuse.put(baseline, values)
            original = bound_read(baseline)
            if not values.keys() <= original.keys():
                raise RuntimeError('fixture file system changed after disk budget registration')
            # The original fixture record identifies the file systems. Free
            # space since fixture creation also includes unrelated host writes
            # between runs. The write budget applies to the admitted phase.
            self.disk_stage_before = values
            reuse.put(self.out/'disk-stage-before.json', values)
        if self.disk_stage_before.keys() != values.keys():
            raise RuntimeError('fixture file system changed during the admitted phase')
        if sum(max(0, self.disk_stage_before[key]-value) for key, value in values.items()) > 1024*MIB:
            raise RuntimeError('fixture allocated disk budget exceeded')

    def sample(self, now):
        if observe_device(self.fixture) != bound_read(self.out/'device-before.json'):
            raise RuntimeError('DRM node identity changed during the phase')
        self.check_acl_watch()
        self.check_disk()
        if self.fixture.cuda:
            reuse.put(self.out/'gpu-last.json', {'at': now, **self.cuda_telemetry(active=True)})
        elif self.phase == 'vulkan':
            reuse.put(self.out/'gpu-last.json', {'at': now, **observe_gpu(self.fixture, vulkan_probe.PAYLOAD_BYTES)})
        elif self.phase == 'media':
            reuse.put(self.out/'gpu-last.json', {'at': now, **observe_gpu(self.fixture, intel_media_guard.PAYLOAD_BYTES)})
        elif self.phase == 'gpu':
            gpu = observe_gpu(self.fixture, self.spec['gpu_payload_bytes'])
            reuse.put(self.out/'gpu-last.json', {'at': now, **gpu})
        for name in ('command.log', 'events.jsonl', 'events.stderr', 'samples.jsonl'):
            if (self.out/name).stat().st_size > 20*MIB:
                raise RuntimeError('fixture evidence exceeded its per-log budget: '+name)
        path = self.out/'live-binding.json'
        if not path.exists() or (self.out/'stop-intent.json').exists():
            return
        live = bound_read(path)
        try:
            init = reuse.process(live['init']['pid'])
        except FileNotFoundError:
            raise RuntimeError('fixture init died without a registered stop')
        if not reuse.same_execution(live['init'], init):
            raise RuntimeError('fixture init PID/cgroup changed')
        if not live['coverage_verified']:
            return
        if not reuse.same_execution(live['conmon'], reuse.process(live['conmon']['pid'])):
            raise RuntimeError('fixture conmon identity changed')
        ready = bound_read(self.out/'ready.json')
        current = reuse.effective_limits(init['cgroup'], ready['aggregate'])
        current = self.verify_live_guest_limits(live, current, ready['aggregate'], now)
        load = reuse.load_metrics(init['cgroup'])
        if self.guest_policy is None:
            from tools.reuse_guard_policy import GuardPolicy
            self.guest_policy = GuardPolicy(memory_limit=current['memory.max'], task_limit=current['pids.max'],
                                            deadline=now+self.spec['seconds'])
        elif self.guest_policy.memory_limit != current['memory.max']:
            self.guest_policy.memory_limit = current['memory.max']
            self.guest_policy.memory_since = None
        self.guest_policy.task_limit = current['pids.max']
        row = {'available': self.config['reserve_bytes'], 'full': 0, 'some': 0,
               'memory': load['memory'], 'tasks': load['tasks'],
               'oom_events': load['events'].get('oom', 0)+load['events'].get('oom_kill', 0),
               'max_events': load['events'].get('max', 0),
               'pids_events': load['pids_events'].get('max', 0)}
        reason = self.guest_policy.check(row, now=now)
        if reason:
            reuse.put(self.out/'guest-limit-sample.json', {
                'cid': live['cid'], 'init': init, 'observed_monotonic': now, 'memory': load['memory'],
                'events': load['events'], 'pids_events': load['pids_events'], 'guest_limits': current,
                'reason': reason})
            raise RuntimeError('GUEST_'+reason)
        if not self.guest_policy.may_enqueue(row):
            (self.out/'queue-closed').touch()

    def cuda_telemetry(self, *, active=False):
        from tools.hardware_guard_policy import _temperature
        cpu_sensors = [path for sensor in Path('/sys/class/hwmon').glob('hwmon*')
                       if (sensor/'name').read_text().strip() == 'coretemp'
                       for path in sensor.glob('temp*_input')]
        cpu = _temperature(cpu_sensors, 80000)
        cuda = self.fixture.cuda
        prefix = ['/usr/bin/nvidia-smi', '--id='+cuda.uuid]
        output = ['--format=csv,noheader,nounits']
        row = reuse.call([*prefix, '--query-gpu=uuid,pci.bus_id,memory.total,memory.free,temperature.gpu',
                          *output], timeout=.75).strip().split(',')
        uuid, pci, total, free, temp = (item.strip() for item in row)
        total, free, temp = map(int, (total, free, temp))
        if (uuid != cuda.uuid or pci.lower() not in (cuda.pci, '0000'+cuda.pci)
                or total != 16384 or free < 4096 or temp >= 70 or temp < 0):
            raise RuntimeError('V100 GPU identity, temperature or 4 GiB reserve check failed')
        pids = reuse.call([*prefix, '--query-compute-apps=pid', *output], timeout=.75).split()
        if pids:
            running = self.out/'live-binding.json'
            if not active or self.phase != 'gpu' or not running.is_file():
                raise RuntimeError('V100 has a compute context outside the admitted probe')
            bound = bound_read(running)
            for pid in map(int, pids):
                try:
                    cgroup = reuse.process(pid)['cgroup']
                except FileNotFoundError:
                    continue  # The short probe may already have destroyed this context.
                if not reuse.under(cgroup, bound['init']['cgroup']):
                    raise RuntimeError('V100 has a compute context outside the admitted guest')
        return {'uuid': uuid, 'pci': pci, 'memory_total_mib': total, 'memory_free_mib': free,
                'temperature_c': temp, 'compute_pids': pids, 'cpu_temp_millicelsius': cpu}

    def cuda_payload(self, name):
        source = self.out.parent/(name+'-probe')
        owned_directory(source, private=True)
        digests = self.spec[name+'_sha256']
        if {path.name for path in source.iterdir()} != set(digests):
            raise RuntimeError('unexpected files in the CUDA probe directory')
        for filename, digest in digests.items():
            path = source/filename
            if name == 'video':
                v100_media_probe.verify_file(path, digest)
            elif hashlib.sha256(bound_bytes(path)).hexdigest() != digest:
                raise RuntimeError('Tensor Core probe digest mismatch: '+filename)
        return source

    def tensor_guest_path(self):
        return '/var/tmp/'+self.spec['unit']+'-tensor'

    def vulkan_binary(self):
        source = self.out.parent/'build/vulkan_known_answers'
        if not source.is_relative_to(ROOT/'artifacts'):
            raise RuntimeError('Vulkan probe must use its private evidence directory')
        owned_directory(source.parent, private=True)
        data = bound_bytes(source)
        if (not 0 < len(data) <= 1024**2 or not data.startswith(b'\x7fELF')
                or not source.stat().st_mode & stat.S_IXUSR
                or hashlib.sha256(data).hexdigest() != self.spec['probe_sha256']):
            raise RuntimeError('Vulkan probe executable disagrees with its audited build')
        return source

    def vulkan_guest_path(self):
        return '/tmp/'+self.spec['unit']+'-probe'

    def vulkan_empty_path_command(self):
        return ['/bin/sh', '-c', 'test ! -e "$1" && test ! -L "$1"',
                'unifier-vulkan', self.vulkan_guest_path()]

    def vulkan_environment(self):
        private = self.vulkan_guest_path()+'-private'
        return {
            'LC_ALL': 'C', 'HOME': private, 'XDG_CACHE_HOME': private+'/cache',
            'XDG_DATA_HOME': private+'/data', 'XDG_CONFIG_HOME': private+'/config',
            'XDG_RUNTIME_DIR': private+'/runtime', 'VK_DRIVER_FILES': vulkan_probe.ICD,
            'VK_LOADER_LAYERS_DISABLE': '~implicit~', 'MESA_SHADER_CACHE_DISABLE': 'true',
        }

    def intel_media_packages(self, plan, cid):
        run_root = self.out.parent
        if not run_root.is_relative_to(ROOT/'artifacts'):
            raise RuntimeError('Intel media preparation must use its own private evidence directory')
        owned_directory(run_root, private=True)
        path_file = run_root/'shared-path.txt'
        shared = Path(bound_bytes(path_file).decode().strip())
        if shared.parent != self.fixture.shared or not shared.name.startswith('vaapi-'):
            raise RuntimeError('Intel media archive location is outside the private fixture share')
        archive = shared/'archives'
        owned_directory(shared, private=True)
        owned_directory(archive, private=True)
        expected = intel_media_guard.expected_archives(bound_read(run_root/'query/result.json'),
                    bound_read(run_root/'prefetch.json'), plan, cid, archive)
        return archive, expected

    def cleanup_config(self):
        config = self.active_config(optional=True)
        if (config['cid'] and (self.out/'start-intent.json').exists()
                and not (self.out/'live-binding.json').exists()):
            current = reuse.info(config, hardware=self, timeout=self.cleanup_timeout(5, guest=True))
            if current['State']['Running']:
                # Only the exact launched CID and a verified PID/scope may
                # be registered if start was interrupted before its callback.
                intent = bound_read(self.out/'start-intent.json')
                if (intent.get('cid') != config['cid'] or intent['before']['Running']
                        or intent['before']['StartedAt'] == current['State']['StartedAt']):
                    raise RuntimeError('CLEANUP_UNKNOWN: start intent does not match this execution')
                reuse.register_live_binding(config, self.out, current, new_start=True,
                                            timeout=self.cleanup_timeout(5, guest=True))
        return config

    def cleanup(self, *, rescue=False):
        try:
            result = reuse.cleanup(self.config, self.out, hardware=self)
        except Exception:
            if (self.out/'start-intent.json').exists():
                self.latch('cleanup identity or stopped state could not be established')
            raise
        if not (self.out/'non-target-before.json').exists():
            return result
        current = snapshot(self.cleanup_timeout)
        config = self.active_config(optional=True)
        cid = config['cid']
        before = bound_read(self.out/'non-target-before.json')
        after = {key: value for key, value in current.items() if key != cid}
        expected = {key: value for key, value in before.items() if key != cid}
        reuse.put(self.out/'native-after.json', current)
        if cid is not None and (cid not in current or current[cid]['state']['Running'] is not False):
            self.latch('target stopped state is unverified')
            raise RuntimeError('target stopped state is unverified')
        if cid is None and any(row['name'] == self.fixture.name for row in current.values()):
            self.latch('created container CID was not returned')
            raise RuntimeError('CLEANUP_UNKNOWN: created container CID was not returned')
        if after != expected:
            raise RuntimeError('non-target container inventory changed')
        if self.fixture.cuda:
            reuse.put(self.out/'gpu-after.json', self.cuda_telemetry())
        transition_path = self.out/AMD_DNF_LIMIT_INTENT
        if self.spec.get('guest_profile') in AMD_DNF_MEMORY_PROFILES and transition_path.exists():
            transition = bound_read(transition_path)
            scope = transition['scope']
            final = reuse.unit(scope['Id'], timeout=self.cleanup_timeout(5))
            if final.get('ControlGroup') or reuse.cgroup_dir(scope['ControlGroup']).exists():
                self.latch('AMD DNF transient quota scope still exists after stop')
                raise RuntimeError('CLEANUP_UNKNOWN: AMD DNF transient quota scope still exists')
            value = reuse.info(config, hardware=self, timeout=self.cleanup_timeout(5))
            if value['State']['Running'] or value['State']['Pid'] != 0:
                self.latch('AMD DNF stopped configuration did not pass post-transition inspection')
                raise RuntimeError('CLEANUP_UNKNOWN: AMD DNF stopped configuration failed inspection')
            reuse.put(self.out/'amd-guest-memory-after.json', {
                'cid': cid, 'scope_final': final, 'transient_scope_gone': True,
                'saved_memory': self.resources['memory'], 'saved_memory_swap': self.resources['memory_swap'],
                'state': value['State']})
        command_path = self.out/'command-result.json'
        if (not rescue and self.phase in BOOTSTRAP_PHASES and command_path.exists()
                and bound_read(command_path)['exit_code'] == 0 and not (self.out/'abort.json').exists()):
            binding = bound_read(self.out/'official-binding.json')
            if self.phase == 'create':
                native_cid = self.create_event
            else:
                proof = 'resume-proof.json' if self.phase == 'resume' else 'amd-bootstrap-before.json'
                native_cid = bound_read(self.out/proof)['cid']
            if not self.start_seen or native_cid != cid or binding['cid'] != cid:
                raise RuntimeError('fixture registration is not uniquely bound to the completed bootstrap')
            absent(self.binding_path)
            reuse.put(self.binding_path, binding)
        return result

    def latch(self, reason):
        path = self.base/'.guard-abort.json'
        if not path.exists():
            reuse.put(path, {'attempt': str(self.out), 'reason': reason})


class HardwareRuntime(PodmanRuntime):
    """Uses the formal runtime's contract with a fixed resource envelope."""
    def __init__(self, attempt):
        super().__init__(runner=self.checked_run, popen=self.checked_popen, check_rootless=False)
        self.attempt = attempt
        self.fixture = attempt.fixture
        self.inventory_record = None

    def identity(self, name, project, env_id):
        config = self.attempt.active_config()
        if (name not in {self.fixture.name, config['cid']}
                or project != str(self.fixture.root) or env_id != self.fixture.env_id):
            raise RuntimeError('runtime request is outside the approved fixture')
        return config

    def checked_run(self, argv, *, capture, timeout):
        attempt = self.attempt
        verb = argv[1]
        stopping = verb == 'stop' or (verb == 'inspect' and (attempt.out/'stop-intent.json').exists())
        if stopping:
            reuse.verify_permission(attempt.config, attempt.out, enqueue=False)
        else:
            verify_hardware_permission(attempt)
        cid = attempt.active_config(optional=True)['cid']
        if verb == 'inspect' and argv[2:4] == ['--format', 'json'] and len(argv) == 5:
            if argv[4] != self.fixture.name and argv[4] != cid:
                raise RuntimeError('inspect is outside the fixed fixture')
            if cid:
                argv = [*argv[:4], cid]
        elif verb == 'image' and argv[2:] == ['inspect', '--format', '{{json .}}', self.fixture.reference]:
            pass
        elif verb == 'start' and argv[2:] == [cid] and cid is not None:
            owned_directory(attempt.hooks, private=True)
            if next(attempt.hooks.iterdir(), None) is not None:
                raise RuntimeError('private OCI hooks directory is no longer empty')
            argv = ['podman', '--hooks-dir='+str(attempt.hooks), *argv[1:]]
        elif verb == 'stop' and argv[2:] == ['--time', '8', cid] and cid is not None:
            pass
        elif verb == 'exec':
            argv = self.pin_exec(argv)
        elif verb == 'unshare' and self.inventory_record is not None:
            self.verify_inventory_command(argv)
        else:
            raise RuntimeError('unsupported hardware test runtime action: '+verb)
        native_timeout = (attempt.cleanup_timeout(min(timeout or 12, 12)) if stopping else
                          command_timeout(attempt, timeout))
        return _run_podman(argv, capture=capture, timeout=native_timeout)

    def pin_exec(self, argv):
        config = self.attempt.active_config()
        # Match the actual Podman operand, never guest argv data.
        first = 2
        while first < len(argv) and argv[first].startswith('-'):
            flag = argv[first]
            if flag in {'--user', '--preserve-fds', '--workdir', '--env', '--env-file'}:
                first += 2
            elif flag in {'-i', '-t'}:
                first += 1
            else:
                raise RuntimeError('unknown option in guarded guest exec')
        if first >= len(argv) or argv[first] not in {self.fixture.name, config['cid']}:
            raise RuntimeError('guest exec is outside the approved CID')
        return [*argv[:first], config['cid'], *argv[first+1:]]

    def checked_popen(self, argv, **kwargs):
        attempt = self.attempt
        verify_hardware_permission(attempt)
        config = attempt.active_config()
        reuse.capture_binding(config, attempt.out, hardware=attempt)
        if (argv[:2] != [sys.executable, SCRIPT_PATH] or argv[3:6] != ['--', 'podman', 'exec']
                or not argv[2].isdigit()):
            raise RuntimeError('hardware runtime only admits the formal guest control wrapper')
        return subprocess.Popen([*argv[:4], *self.pin_exec(argv[4:])], **kwargs)

    def create(self, spec):
        attempt = self.attempt
        f = self.fixture
        if (attempt.phase != 'create' or spec != fixture_create_spec(f)
                or bound_read(attempt.once).get('attempt') != str(attempt.out)
                or (attempt.out/'create-intent.json').exists()):
            raise RuntimeError('formal creation is outside the one-time approved contract')
        if self.inspect(f.name) is not None or observe_device(f) != bound_read(attempt.out/'device-before.json'):
            raise RuntimeError('approved name or device changed immediately before create')
        if next(attempt.hooks.iterdir(), None) is not None:
            raise RuntimeError('the private OCI hooks directory must remain empty')
        argv = fixture_create_argv(f, attempt.hooks, spec)
        verify_hardware_permission(attempt)
        if f.cuda:
            verify_cuda_inputs(f)
        reuse.put(attempt.out/'create-intent.json', {'at_ns': time.time_ns(), 'argv': argv})
        result = _run_podman(argv, capture=True, timeout=command_timeout(attempt, 60))
        cid = result.stdout.strip()
        if result.returncode or not CID.fullmatch(cid):
            raise RuntimeError('approved Podman create failed: '+result.stderr[-2000:])
        reuse.put(attempt.out/'created-container.json', {'cid': cid})
        value = reuse.info(attempt.active_config(), hardware=attempt)
        if value['State']['Running'] or value['State']['Pid']:
            raise RuntimeError('Podman create did not return a stopped fixture')
        return cid

    def remove(self, *_args, **_kwargs):
        raise RuntimeError('approved and historical containers must be retained')

    def verify_created(self, spec, expected_container_id):
        attempt = self.attempt
        if attempt.phase == 'amd-bootstrap-recover':
            return self.verify_amd_interrupted_create(spec, expected_container_id)
        if (attempt.phase != 'resume' or expected_container_id != INTEL_RESUME_CID
                or spec != fixture_create_spec(self.fixture)):
            raise RuntimeError('only the approved Intel bootstrap recovery may verify an incomplete create')
        verify_hardware_permission(attempt)
        marker = bound_read(attempt.resume_marker())
        if (marker.get('attempt') != str(attempt.out) or marker.get('cid') != INTEL_RESUME_CID
                or not reuse.same_execution(marker.get('owner'), reuse.process(os.getpid()))):
            raise RuntimeError('Intel recovery is outside its guarded worker')
        config = attempt.active_config()
        observed = reuse.info(config, hardware=attempt)['State']
        expected_error = INTEL_RETRY_START_ERROR if 'retry_from' in attempt.spec else ''
        if observed['Status'] != 'created' or observed.get('Error') != expected_error:
            raise RuntimeError('Intel recovery requires the same never-started CID')
        return super().verify_created(spec, expected_container_id)

    def verify_amd_interrupted_create(self, spec, expected_container_id):
        attempt = self.attempt
        if expected_container_id != AMD_BOOTSTRAP_CID or spec != fixture_create_spec(self.fixture):
            raise RuntimeError('AMD bootstrap is outside the approved exact fixture')
        verify_hardware_permission(attempt)
        intent = bound_read(attempt.base/AMD_BOOTSTRAP_MARKER)
        proof = bound_read(attempt.out/'amd-bootstrap-before.json')
        if (intent.get('attempt') != str(attempt.out) or intent.get('cid') != AMD_BOOTSTRAP_CID
                or intent.get('original_attempt') != str(AMD_BOOTSTRAP_FROM)
                or not reuse.same_execution(intent.get('owner'), reuse.process(os.getpid()))):
            raise RuntimeError('AMD bootstrap is outside its registered worker')
        store = EnvironmentStore(self.fixture.root)
        absent(attempt.record_path)
        absent(store.inventory_path(self.fixture.env_id))
        value = reuse.info(attempt.active_config(), hardware=attempt)
        validate_amd_stopped_layout(value, proof['stopped'])
        return container_state_from_inspect(self.fixture.name, value)

    def start(self, name, *, project, env_id):
        attempt = self.attempt
        config = self.identity(name, project, env_id)
        verify_hardware_permission(attempt)
        before = reuse.info(config, hardware=attempt)
        if before['State']['Running'] or (attempt.out/'start-intent.json').exists():
            raise RuntimeError('fixture accepts exactly one start per admitted phase')
        if self.fixture.cuda:
            verify_cuda_inputs(self.fixture)
            attempt.cuda_telemetry()
        if attempt.phase in PACKAGE_MAINTENANCE_PHASES:
            attempt.verify_recovery_claim()
        if attempt.spec.get('guest_profile') == AMD_DNF_PROFILE:
            attempt.claim_amd_dnf_retry()
        elif attempt.spec.get('guest_profile') in AMD_DNF_INSTALL_PROFILES:
            attempt.claim_amd_offline_install()
        reuse.put(attempt.out/'start-intent.json', {'cid': config['cid'], 'at_ns': time.time_ns(),
                                                   'before': before['State']})
        super().start(config['cid'], project=project, env_id=env_id)
        binding = reuse.capture_binding(config, attempt.out, new_start=True, hardware=attempt)
        proc = Path('/proc') / str(binding['init']['pid'])
        mappings = [row.split() for row in (proc/'uid_map').read_text().splitlines()]
        if not mappings or mappings[0] != ['0', str(self.fixture.uid), '1']:
            raise RuntimeError('guest root is not mapped to the approved ordinary user')
        if self.fixture.cuda:
            dev = proc/'root/dev'
            actual = {str(Path('/dev')/entry.name): entry.lstat().st_rdev
                      for entry in dev.iterdir() if entry.name.startswith('nvidia')}
            if actual != bound_read(attempt.out/'device-before.json') or (dev/'dri').exists():
                raise RuntimeError('running fixture has unexpected NVIDIA or DRM devices')
            reuse.put(attempt.out/'guest-device.json', {'mapping': mappings[0], 'nodes': actual})
        else:
            path = proc/'root/dev/dri'
            node = (path/'renderD128').stat()
            if ({p.name for p in path.iterdir()} != {'renderD128'}
                    or not stat.S_ISCHR(node.st_mode) or (os.major(node.st_rdev), os.minor(node.st_rdev)) != (226, 128)):
                raise RuntimeError('running fixture has an unexpected DRM device set')
            reuse.put(attempt.out/'guest-device.json', {'mapping': mappings[0], 'rdev': node.st_rdev})
        if attempt.spec.get('guest_profile') in AMD_DNF_MEMORY_PROFILES:
            attempt.apply_amd_dnf_budget(binding)
        if attempt.spec.get('qq_gui'):
            attempt.apply_qq_gui_budget(binding)
        attempt.set_package_memory_high(binding)

    def stop(self, name, *, project, env_id):
        config = self.identity(name, project, env_id)
        reuse.verify_permission(config, self.attempt.out, enqueue=False)
        reuse.put(self.attempt.out/'stop-intent.json', {'cid': config['cid'], 'at_ns': time.time_ns()})
        super().stop(config['cid'], project=project, env_id=env_id)

    def exec(self, name, argv, *, project, env_id, **kwargs):
        config = self.identity(name, project, env_id)
        attempt = self.attempt
        if attempt.phase == 'vulkan' and argv != ['uname', '-r']:
            path = attempt.vulkan_guest_path()
            commands = {tuple(attempt.vulkan_empty_path_command()), (path,),
                        ('/usr/bin/rm', '--', path)}
            expected_env = attempt.vulkan_environment() if argv == [path] else {}
            if (tuple(argv) not in commands or kwargs.get('env') != expected_env
                    or kwargs.get('capture') is not True or kwargs.get('timeout') != 10
                    or kwargs.get('user') is not None or kwargs.get('workdir') is not None):
                raise RuntimeError('guest operation is outside the bounded Intel Vulkan probe')
            if argv == [path]:
                register_intent(attempt.out/'vulkan-probe-intent.json', {'cid': config['cid'], 'argv': argv})
        if self.fixture.cuda and argv and argv[0] in (V100_PROBE, attempt.tensor_guest_path()+'/'+V100_TENSOR_FILES[0]):
            if (attempt.phase != 'gpu' or argv != cuda_probe_argv(attempt)
                    or kwargs.get('env') or kwargs.get('user')
                    or kwargs.get('capture') is not True or kwargs.get('timeout') != 10):
                raise RuntimeError('only the fixed CUDA probe and payload are admitted')
            attempt.cuda_telemetry()
            register_intent(attempt.out/'cuda-probe-intent.json', {'cid': config['cid'], 'argv': argv})
        if self.attempt.phase in ('recover', 'media-recover') and argv not in (
                ['uname', '-r'], ['dpkg', '--audit'], inventory_argv('debian13'),
                ['sh', '-c', package_journal.SCRIPT, 'unifier-package-journal', 'debian13', 'probe']):
            raise RuntimeError('guest command is outside the Intel package recovery')
        if self.attempt.phase == 'native-repair' and argv != ['uname', '-r']:
            paths = [entry['path'] for entry in self.attempt.repair_cache()]
            reads = (*INTEL_REPAIR_READS, ['stat', '--format=%f %d %i %s %Y %Z', '--', *paths],
                     ['sha256sum', '--', *paths])
            if (argv not in reads or kwargs.get('env') != intel_repair_environment()
                    or kwargs.get('user') != '0' or kwargs.get('workdir') is not None
                    or kwargs.get('capture') is not True):
                raise RuntimeError('guest command is outside the Intel native repair observation')
        reuse.capture_binding(config, self.attempt.out, hardware=self.attempt)
        return super().exec(config['cid'], argv, project=project, env_id=env_id, **kwargs)

    def copy_in(self, name, host_path, container_path, *, project, env_id):
        attempt = self.attempt
        config = self.identity(name, project, env_id)
        verify_hardware_permission(attempt)
        if attempt.phase == 'vulkan':
            source = attempt.vulkan_binary()
            if Path(host_path) != source or container_path != attempt.vulkan_guest_path():
                raise RuntimeError('copy-in is outside the bounded Intel Vulkan probe')
            reuse.capture_binding(config, attempt.out, hardware=attempt)
            self._check_copy(source, config['cid'], container_path)
            return
        source = ROOT/'fixtures/hardware/drm_egl_probe.py'
        if (attempt.phase in BUILTIN_PHASES or attempt.phase == 'native-repair' or Path(host_path) != source
                or container_path not in {'/tmp/drm_egl_probe.py', '/tmp/unifier-hardware-drm_egl_probe.py'}):
            raise RuntimeError('copy-in only permits the worktree probe into the fixed fixture')
        reuse.capture_binding(config, attempt.out, hardware=attempt)
        self._check_copy(source, config['cid'], container_path)

    def _check_copy(self, source, cid, target, *, timeout=15):
        argv = ['podman', 'cp', '--pause=false', str(source), f'{cid}:{target}']
        result = _run_podman(argv, capture=True, timeout=command_timeout(self.attempt, timeout))
        if result.returncode:
            raise RuntimeError('fixture probe copy failed: '+result.stderr[-1500:])

    def package_exec(self, name, argv, *, project, env_id, timeout, **kwargs):
        config = self.identity(name, project, env_id)
        attempt = self.attempt
        if attempt.spec['fixture'] in ('arch-intel', 'arch-amd'):
            verify_hardware_permission(attempt)
            pinned = self.admit_arch_package_step(config, argv, kwargs)
            return super().package_exec(config['cid'], pinned, project=project, env_id=env_id,
                                        timeout=command_timeout(attempt, timeout), **kwargs)
        if attempt.phase == 'recover':
            checkpoint = ['sh', '-c', package_journal.SCRIPT, 'unifier-package-journal',
                          'debian13', 'checkpoint']
            verify_hardware_permission(attempt)
            if argv != checkpoint or kwargs.get('interactive') or kwargs.get('stream'):
                raise RuntimeError('only the exact Debian package database checkpoint is admitted')
            register_intent(attempt.out/'checkpoint-intent.json', {'argv': checkpoint, 'cid': config['cid']})
            return super().package_exec(config['cid'], argv, project=project, env_id=env_id,
                                        timeout=command_timeout(attempt, timeout), **kwargs)
        if attempt.phase == 'native-repair':
            verify_hardware_permission(attempt)
            self.admit_native_repair_step(config, argv, kwargs)
            return super().package_exec(config['cid'], argv, project=project, env_id=env_id,
                                        timeout=command_timeout(attempt, timeout), **kwargs)
        plan = attempt.spec.get('package_plan')
        if not plan or attempt.phase != 'prepare' or kwargs.get('interactive'):
            raise RuntimeError('native package mutation requires a reviewed fixture package plan')
        verify_hardware_permission(attempt)
        requests = plan['requests']
        names = package_names(attempt.spec['fixture'], requests)
        admitted = [command for command, _ in install_commands(self.fixture.env_id, names)]
        if argv not in admitted:
            raise RuntimeError('package command diverges from the approved preparation plan')
        index = admitted.index(argv)
        pinned = install_commands(self.fixture.env_id, requests)[index][0]
        journal = attempt.out/f'admitted-package-step-{index}.json'
        if attempt.spec.get('intel_media_cache'):
            archive, expected = attempt.intel_media_packages(plan, config['cid'])
            if index == 0:
                pinned = ['/usr/bin/timeout', '--kill-after=3s', '85s', '/usr/bin/apt-get', 'update',
                          '-o', 'APT::Update::Error-Mode=any', '-o', 'Acquire::Languages=none',
                          '-o', 'Acquire::Retries=0', '-o', 'Acquire::http::Dl-Limit=512',
                          '-o', 'Acquire::http::Proxy=DIRECT', '-o', 'Acquire::http::AllowRedirect=false',
                          '-o', 'Acquire::http::Pipeline-Depth=0']
            else:
                lookup = self.exec(config['cid'], ['/usr/bin/apt-get', '--print-uris', '--download-only',
                                   '--yes', '--no-install-recommends', '--no-remove',
                                   '-o', 'Dir::Cache::archives='+str(archive), 'install', *requests],
                                   project=project, env_id=env_id, workdir=None, env={'LC_ALL':'C'},
                                   capture=True, timeout=25)
                reuse.put(attempt.out/'apt-cache-lookup.json',
                          {'stdout': lookup.stdout, 'stderr': lookup.stderr, 'exit_code': lookup.returncode})
                if lookup.returncode != 0:
                    raise RuntimeError('cannot inspect APT archive lookup before the Intel media install')
                scratch = '/var/tmp/'+attempt.spec['unit']+'-archives'
                def execute(args):
                    return self.exec(config['cid'], args, project=project, env_id=env_id,
                                     workdir=None, env={'LC_ALL':'C'}, capture=True, timeout=25)
                listing = execute(['/usr/bin/stat', '--', str(archive)])
                reuse.put(attempt.out/'apt-share-visibility.json',
                          {'stdout': listing.stdout, 'stderr': listing.stderr, 'exit_code': listing.returncode})
                if execute(['/bin/sh', '-c', 'test ! -e "$1" && test ! -L "$1"', 'media-archives', scratch]).returncode:
                    raise RuntimeError('Intel media guest archive path already exists')
                filenames = sorted(intel_media_guard.archive_name(name, *identity)
                                   for name, identity in expected.items())
                if sorted(path.name for path in archive.iterdir()) != filenames:
                    raise RuntimeError('Intel media transfer directory has unexpected files')
                self._check_copy(archive, config['cid'], scratch, timeout=45)
                paths = [scratch+'/'+filename for filename in filenames]
                hashes = execute(['/usr/bin/sha256sum', '--', *paths])
                receipt = {line.split()[1]: line.split()[0] for line in hashes.stdout.splitlines()}
                prefetch = bound_read(attempt.out.parent/'prefetch.json')
                expected_hashes = {scratch+'/'+row['filename']:row['sha256'] for row in prefetch['packages']}
                if hashes.returncode or receipt != expected_hashes:
                    raise RuntimeError('Intel media guest archive digest differs after scoped copy')
                reuse.put(attempt.out/'apt-transferred-cache.json', receipt)
                # APT receives only authenticated local files. There are no
                # archive sources in this command's private option set.
                options = ['--no-install-recommends', '--no-remove',
                           '-o', 'Dir::Etc::sourcelist=/dev/null',
                           '-o', 'Dir::Etc::sourceparts=-']
                result = self.exec(config['cid'], ['/usr/bin/apt-get', '--simulate', *options,
                                                   'install', *paths],
                                   project=project, env_id=env_id, workdir=None, env={'LC_ALL':'C'},
                                   capture=True, timeout=25)
                reuse.put(attempt.out/'apt-final-simulation.json',
                          {'stdout': result.stdout, 'stderr': result.stderr, 'exit_code': result.returncode})
                if result.returncode != 0:
                    raise RuntimeError('Intel media cache-only APT simulation failed')
                intel_media_guard.check_simulated_closure(result.stdout, expected)
                pinned = ['/usr/bin/apt-get', *options, 'install', '-y', *paths]
        if self.fixture.driver == 'radeon':
            if (attempt.spec.get('guest_profile') not in AMD_DNF_INSTALL_PROFILES
                    or config['cid'] != AMD_BOOTSTRAP_CID or plan != AMD_DNF_EXACT_PLAN
                    or pinned != ['dnf', 'install', '-y', *AMD_DNF_EXACT_PLAN['requests']]
                    or kwargs.get('interactive') is not False or kwargs.get('stream') is not False
                    or kwargs.get('env') != native_env('fedora44') or not callable(kwargs.get('on_guest'))):
                raise RuntimeError('AMD package mutation requires the fixed offline formal transaction')
            evidence = bound_read(attempt.out/'amd-offline-install-ready.json')
            before = bound_read(attempt.out/'amd-offline-install-before.json')
            claim = bound_read(attempt.base/amd_install_marker(attempt.spec.get('guest_profile')))
            if (claim.get('attempt') != str(attempt.out) or claim.get('binding') != AMD_DNF_BINDING
                    or not reuse.same_execution(claim.get('owner'), reuse.process(os.getpid()))
                    or evidence.get('binding') != AMD_DNF_BINDING
                    or evidence.get('prefetch_sha256') != before['prefetch_sha256']
                    or claim.get('prefetch_sha256') != before['prefetch_sha256']
                    or claim.get('refresh_sha256') != before.get('refresh_sha256')
                    or evidence.get('refresh_sha256') != before.get('refresh_sha256')
                    or evidence.get('signing_fingerprint') != AMD_DNF_SIGNING_FINGERPRINT
                    or evidence.get('verified_rpms') != before['packages']
                    or evidence.get('query_sha256') != before['query_sha256']
                    or evidence.get('solver_plan') != bound_read(AMD_DNF_SUCCEEDED_QUERY/'result.json')['plan']):
                raise RuntimeError('AMD package submission lacks fresh signed-cache evidence')
            amd_install_submission_time(attempt)
            reuse.capture_binding(config, attempt.out, hardware=attempt)
            pinned = [pinned[0], *AMD_DNF_OFFLINE_OPTIONS, *pinned[1:]]
            kwargs['env'] = {**kwargs['env'], 'DNF5_PLUGINS_DIR': ''}
            timeout = min(timeout, AMD_DNF_INSTALL_NATIVE_TIMEOUT)
        register_intent(journal, {'logical_argv': argv, 'native_argv': pinned, 'plan': plan})
        if self.fixture.driver == 'i915':
            kwargs['env'] = {**kwargs['env'], 'DPKG_DEB_THREADS_MAX': '1'}
        result = super().package_exec(config['cid'], pinned, project=project, env_id=env_id,
                                      timeout=command_timeout(attempt, timeout), **kwargs)
        if attempt.spec.get('intel_media_cache') and index == 1 and result.returncode == 0:
            removal = execute(['/usr/bin/rm', '-r', '--', scratch])
            if removal.returncode != 0:
                raise RuntimeError('Intel media owned guest archive removal failed')
        return result

    def admit_arch_package_step(self, config, argv, options):
        attempt = self.attempt
        plan = attempt.arch_package_plan()
        (command, _), = install_commands('arch', plan['requests'])
        bootstrap = (['pacman-key', '--init'], ['pacman-key', '--populate', 'archlinux'])
        if (options.get('interactive') or options.get('stream') or options.get('env') != native_env('arch')
                or (argv != command and argv not in bootstrap)):
            raise RuntimeError('Arch package mutation differs from the fixed offline plan')
        root = arch_gpu_packages.guest_repository(attempt.spec['unit'])
        guest_config = str(root/'pacman.conf')
        proof = bound_read(attempt.out/'arch-package-ready.json')
        expected_files = len(plan['repositories']) + 2*len(plan['packages'])
        if (proof.get('cid') != config['cid'] or proof.get('planned_sha256') != attempt.spec['arch_plan_sha256']
                or proof.get('guest_config') != guest_config or proof.get('verified_files') != expected_files):
            raise RuntimeError('Arch guest package inputs do not match their verified closure')

        def guest(args):
            found = self.exec(config['cid'], args, project=str(self.fixture.root), env_id='arch',
                              workdir=None, env={'LC_ALL': 'C'}, capture=True, timeout=10)
            if found.returncode:
                raise RuntimeError('Arch offline configuration verification failed')
            return found.stdout.splitlines()

        conf = ['pacman-conf', '--config', guest_config]
        if (guest(['sha256sum', guest_config]) != [f"{proof['guest_config_sha256']}  {guest_config}"]
                or guest([*conf, '--repo-list']) != ['core', 'extra']):
            raise RuntimeError('Arch native package configuration changed')
        for repo in ('core', 'extra'):
            policy = guest([*conf, '--repo', repo, 'SigLevel']) or guest([*conf, 'SigLevel'])
            if (guest([*conf, '--repo', repo, 'Server']) != [f'file://{root}/{repo}']
                    or not {'PackageRequired', 'PackageTrustedOnly'} <= set(policy)):
                raise RuntimeError('Arch repository is not offline with mandatory trusted package signatures')
        pinned = [*argv, '--config', guest_config] if argv == command else argv
        step = 'install' if argv == command else argv[1].removeprefix('--')
        register_intent(attempt.out/f'arch-package-{step}-intent.json',
                        {'logical_argv': argv, 'native_argv': pinned, 'proof': proof})
        return pinned

    def admit_native_repair_step(self, config, argv, options):
        out = self.attempt.out
        report = bound_read(out/'native-maintenance.json')
        archive = report.get('verified_archive', {})
        if (options.get('interactive') is not False or options.get('stream') is not False
                or options.get('env') != intel_repair_environment() or not callable(options.get('on_guest'))
                or archive.get('path') != INTEL_REPAIR_ARCHIVE or archive.get('size') != INTEL_REPAIR_SIZE
                or archive.get('sha256') != INTEL_REPAIR_SHA256):
            raise RuntimeError('native repair requires the checked cache and exact single-thread environment')
        verify_repair_cache_report(report, self.attempt.repair_cache())
        for step, expected in INTEL_REPAIR_STEPS.items():
            path = out/f'native-repair-{step}-intent.json'
            if path.exists():
                continue
            if argv != expected:
                raise RuntimeError('native repair step differs from its reviewed sequence')
            register_intent(path, {'argv': expected, 'cid': config['cid'], 'archive_sha256': INTEL_REPAIR_SHA256})
            return
        raise RuntimeError('native repair steps have already been submitted')

    def read_inventory_readonly(self, record):
        attempt = self.attempt
        verify_hardware_permission(attempt)
        binding = binding_for(record)
        arch = ((binding['container_id'] == ARCH_RECOVERY_CID and attempt.phase == 'reconcile')
                or (attempt.fixture.env_id == 'arch' and attempt.phase == 'prepare'
                    and 'arch_plan_sha256' in attempt.spec and binding['container_id'] == attempt.active_config()['cid']))
        if not arch and (attempt.phase not in ('recover', 'media-recover') or binding != INTEL_RECOVER_BINDING):
            raise RuntimeError('stopped inventory requires the admitted recovery binding')
        if not (attempt.out/'stop-intent.json').exists():
            raise RuntimeError('recovery inventory requires the registered stop')
        state = reuse.info(attempt.active_config(), hardware=attempt)['State']
        if state['Running'] or state['Pid'] != 0:
            raise RuntimeError('recovery inventory requires the exact stopped CID')
        self.inventory_record = record
        try:
            return super().read_inventory_readonly(record)
        finally:
            self.inventory_record = None

    def verify_inventory_command(self, argv):
        expected = ['podman', 'unshare', sys.executable, str(ROOT/'src/unifier/inventory_probe.py'), '--record']
        if (len(argv) != 10 or argv[:5] != expected or argv[6] != '--journal'
                or argv[7] != str(self.attempt.storage_observation_path())
                or argv[8] != '--token' or not re.fullmatch('[0-9a-f]{32}', argv[9])):
            raise RuntimeError('stopped inventory command is outside the approved recovery')
        record = self.inventory_record
        expected_record = {key: record[key] for key in
                           ('id', 'target', 'schema_version', 'environment_uuid', 'generation')}
        expected_record['runtime'] = {key: record['runtime'][key] for key in ('container_id', 'project')}
        if json.loads(argv[5]) != expected_record:
            raise RuntimeError('stopped inventory record is outside the approved recovery')


def validate_amd_stopped_layout(value, expected):
    state, config = value['State'], value['Config']
    # Podman 4.9 may omit this diagnostic on later stopped inspections.
    if expected.get('StoppedByUser') is True and 'StoppedByUser' not in state:
        expected = {key: value for key, value in expected.items() if key != 'StoppedByUser'}
    if (state != expected
            or state.get('StoppedByUser') is not expected.get('StoppedByUser')
            or state['Status'] != 'exited' or state['Pid'] != 0
            or value.get('RestartCount') != 0 or value.get('ExecIDs')
            or value.get('Pod') or value.get('Dependencies')
            or value.get('Path') != '/run/podman-init'
            or value.get('Args') != ['--', SLEEPER, 'infinity']
            or config.get('Entrypoint') not in (SLEEPER, [SLEEPER]) or config.get('Cmd') != ['infinity']
            or config.get('StopTimeout') != 8 or value['HostConfig'].get('Init') is not True):
        raise RuntimeError('AMD interrupted bootstrap state or sleeper layout changed')


def write_scope_limit(path, before, final):
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if os.read(fd, 64).decode('ascii').strip() != before:
            raise RuntimeError('scope limit changed before setting '+path.name)
        os.lseek(fd, 0, os.SEEK_SET)
        payload = str(final).encode('ascii')
        if os.write(fd, payload) != len(payload):
            raise RuntimeError('scope limit write was incomplete: '+path.name)
    finally:
        os.close(fd)
    if path.read_text().strip() != str(final):
        raise RuntimeError('scope limit did not take effect: '+path.name)


def verify_hardware_permission(attempt):
    reuse.verify_permission(attempt.config, attempt.out)
    attempt.check_acl_watch()
    if observe_device(attempt.fixture) != bound_read(attempt.out/'device-before.json'):
        raise RuntimeError('DRM device identity changed before executing the fixture')


def intel_repair_environment():
    return {**native_env('debian13'), 'DPKG_DEB_THREADS_MAX': '1'}


def amd_offline_libraries():
    parent = Path(AMD_DNF_RETRY_COMMAND[2]).parent
    owned_directory(parent, private=True)
    for name, digest in AMD_DNF_LIBRARIES.items():
        if hashlib.sha256(bound_bytes(parent/name)).hexdigest() != digest:
            raise RuntimeError('AMD isolated DNF controller dependency changed: '+name)
    return runpy.run_path(str(parent/'fedora-cache-controller.py'))


def verify_amd_host_prefetch_exit(directory, exit_code, current):
    owned_directory(directory, private=True)
    for name in ('abort.json', 'start-intent.json', 'live-binding.json'):
        absent(directory/name)
    spec = bound_read(directory/'spec.json')
    command = bound_read(directory/'command-result.json')
    receipt = bound_read(directory/'receipt.json')
    cleanup = bound_read(directory/'cleanup.json')
    if (validate_spec(spec) != FIXTURES['amd'] or spec.get('phase') != 'prepare'
            or 'guest_profile' in spec or 'package_plan' in spec
            or command.get('exit_code') != exit_code or receipt.get('reason') is not None
            or receipt.get('command_result') != command or receipt.get('cleanup') != cleanup
            or cleanup.get('status') != 'NO_START' or cleanup['worker_final'].get('MainPID') != '0'
            or cleanup['worker_final'].get('ControlGroup') != ''
            or bound_read(directory/'native-after.json') != current):
        raise RuntimeError('AMD guarded host prefetch did not finish with the unchanged stopped fixture')


def verify_amd_prefetch_continuation(report):
    previous = {}
    for name, digest in AMD_DNF_PREFETCH_PREVIOUS_HASHES.items():
        raw = bound_bytes(AMD_DNF_PREFETCH_ORIGINAL/name)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError('AMD prefetch interrupted receipt bytes changed: '+name)
        previous[name] = json.loads(raw)
    original = previous['result.json']
    if (original.get('status') != 'BLOCKED' or original.get('formal_stop') != 'NO_START'
            or original.get('error') != {'type': 'ValueError', 'detail': AMD_DNF_PREFETCH_TIMEOUT}
            or report.get('valid_until_unix') != original.get('valid_until_unix')
            or report.get('valid_until_unix') != AMD_DNF_PREFETCH_EXPIRY
            or report.get('official') != original.get('official')
            or report.get('metalink_receipt_sha256') != original.get('metalink_receipt_sha256')):
        raise RuntimeError('AMD prefetch continuation does not preserve the stopped timeout and official cutoff')
    continuation = report['continuation']
    if ({key: value for key, value in continuation.items() if key != 'preserved_partial'} != {
            'schema': 1, 'original_out': str(AMD_DNF_PREFETCH_ORIGINAL),
            'original_sha256': AMD_DNF_PREFETCH_PREVIOUS_HASHES, 'original_verified_packages': 26,
            'original_verified_bytes': 55473306,
            'original_timed_out_transport': AMD_DNF_PREFETCH_TIMEOUT_TRANSFER,
            'new_packages': 2, 'new_payload_bytes': 12497961, 'one_shot': True,
    } or report.get('metalink_receipt') != str(AMD_DNF_METALINK)):
        raise RuntimeError('AMD prefetch continuation is missing its exact source and one-shot receipts')
    original_bytes = continuation_bytes = 0
    for row in report['packages']:
        transfer = row['transport']
        pending = row['name'] in AMD_DNF_PREFETCH_REMAINING
        expected_source = 'continuation' if pending else 'original-prefetch'
        if transfer.get('source') != expected_source:
            raise RuntimeError('AMD prefetch continuation changed the allowed remaining package set')
        directory = AMD_DNF_PREFETCH.parent if pending else AMD_DNF_PREFETCH_ORIGINAL
        verify_amd_prefetch_transfer(directory, row, transfer)
        if pending:
            continuation_bytes += row['download_bytes']
        else:
            original_bytes += row['download_bytes']
    if original_bytes != 55473306 or continuation_bytes != 12497961:
        raise RuntimeError('AMD original and continuation RPM byte totals differ')
    timed_out = next(row for row in report['packages'] if row['name'] == 'python3-libs')
    verify_amd_prefetch_transfer(AMD_DNF_PREFETCH_ORIGINAL, timed_out, AMD_DNF_PREFETCH_TIMEOUT_TRANSFER)
    verify_amd_prefetch_residue(Path(timed_out['file']+'.partial'), continuation['preserved_partial'])


def verify_amd_prefetch_transfer(directory, row, transfer):
    name = Path(row['location']).name
    audit = directory/'transfers'/row['repository']
    owned_directory(audit, private=True)
    raw = {}
    for suffix, key in (('.attempt.json', 'attempt_sha256'), ('.curl', 'curl_sha256')):
        content = bound_bytes(audit/(name+suffix))
        if hashlib.sha256(content).hexdigest() != transfer.get(key):
            raise RuntimeError('AMD RPM transfer evidence changed: '+name)
        raw[key] = content
    attempt = json.loads(raw['attempt_sha256'])
    curl, declared_length, encoding = raw['curl_sha256'].decode().splitlines()
    curl = json.loads(curl)
    expected_exit = 28 if transfer == AMD_DNF_PREFETCH_TIMEOUT_TRANSFER else 0
    if (attempt.get('returncode') != expected_exit or curl.get('exitcode') != expected_exit
            or attempt.get('error') is not None or curl.get('http_code') != 200
            or curl.get('ssl_verify_result') != 0 or curl.get('num_redirects') != 0
            or attempt.get('url') != row['url'] or curl.get('url') != row['url']
            or curl.get('url_effective') != row['url'] or encoding not in {'', 'identity'}
            or declared_length not in {'', str(row['download_bytes'])}
            or attempt.get('payload_bytes') != transfer['size_download']
            or curl.get('size_download') != transfer['size_download']
            or curl.get('size_header') != transfer['size_header']
            or attempt.get('completed_unix') != transfer['completed_unix']):
        raise RuntimeError('AMD RPM pinned transport receipt differs: '+name)


def verify_amd_prefetch_residue(source, residue):
    directory = AMD_DNF_PREFETCH.parent/'residue'
    owned_directory(directory, private=True)
    destination = directory/source.name
    identity = AMD_DNF_PREFETCH_PARTIAL_ID
    digest = residue['sha256']
    if (residue != {'source': str(source), 'destination': str(destination), 'bytes': identity[4],
                    'device': identity[0], 'inode': identity[1], 'sha256': digest, 'same_inode': True,
                    'source_unlinked_after_preservation': True}
            or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or bound_read(directory/'migration-intent.json') != {
                'source': str(source), 'destination': str(destination),
                'identity': list(identity), 'sha256': digest}):
        raise RuntimeError('AMD prefetch preserved timeout residue provenance differs')
    absent(source)
    fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if ((before.st_dev, before.st_ino, before.st_mode, before.st_uid,
             before.st_size, before.st_mtime_ns) != identity or before.st_nlink != 1):
            raise RuntimeError('AMD prefetch timeout residue inode or size differs')
        observed = hashlib.sha256()
        remaining = identity[4]
        while remaining:
            block = stream.read(min(remaining, 256*1024))
            if not block:
                raise RuntimeError('AMD prefetch timeout residue became shorter')
            observed.update(block)
            remaining -= len(block)
        after = os.fstat(stream.fileno())
        if (stream.read(1) or observed.hexdigest() != digest or after.st_ctime_ns != before.st_ctime_ns
                or after.st_mtime_ns != before.st_mtime_ns or after.st_size != before.st_size):
            raise RuntimeError('AMD prefetch timeout residue changed during verification')


def validate_amd_package_receipts(query, query_digest, primary_raw, prefetch_raw, metalink_raw, now,
                                  *, historical_at=None):
    primary_digest = hashlib.sha256(primary_raw).hexdigest()
    prefetch_digest = hashlib.sha256(prefetch_raw).hexdigest()
    metalink_digest = hashlib.sha256(metalink_raw).hexdigest()
    primary, prefetch, link = map(json.loads, (primary_raw, prefetch_raw, metalink_raw))
    stage_digest = query['stage_marker_sha256']
    expiry = prefetch.get('valid_until_unix')
    if type(expiry) not in {int, float}:
        raise RuntimeError('AMD offline inputs have an invalid snapshot deadline')
    if historical_at is None:
        if not now+AMD_DNF_INSTALL_NATIVE_TIMEOUT+AMD_DNF_INSTALL_FINISH_RESERVE < expiry <= now+1800:
            raise RuntimeError('AMD offline inputs are stale for installation')
        authenticated_at = now
    else:
        if (type(historical_at) not in {int, float} or not historical_at <= now
                or not historical_at < expiry <= historical_at+1800):
            raise RuntimeError('AMD archived prefetch did not complete before its recorded deadline')
        authenticated_at = historical_at
    if (primary_digest != AMD_DNF_PRIMARY_SHA256 or query_digest != AMD_DNF_QUERY_SHA256
            or primary.get('status') != 'VERIFIED'
            or primary.get('scope') != 'archived-primary-package-checksums'
            or primary.get('install_eligible') is not False
            or primary.get('snapshot_policy') != 'archived-offline-query-only'
            or primary.get('cid') != AMD_BOOTSTRAP_CID or primary.get('query_sha256') != query_digest
            or primary.get('stage_marker_sha256') != stage_digest
            or set(primary.get('primary', {})) != set(AMD_DNF_MIRRORS)
            or prefetch.get('schema') != 1 or prefetch.get('status') != 'VERIFIED'
            or prefetch.get('cid') != AMD_BOOTSTRAP_CID
            or prefetch.get('environment_uuid') != AMD_DNF_BINDING['environment_uuid']
            or type(prefetch.get('generation')) is not int or prefetch['generation'] != 1
            or prefetch.get('scoped_for') != 'amd-fedora44-prefetch'
            or prefetch.get('formal_stop') != 'NO_START'
            or prefetch.get('payload_root') != str(AMD_DNF_PAYLOAD)
            or prefetch.get('package_plan') != AMD_DNF_EXACT_PLAN
            or prefetch.get('package_signatures') != 'pending-native-gpg-verification'
            or prefetch.get('latest') is not False or prefetch.get('query_sha256') != query_digest
            or prefetch.get('stage_marker_sha256') != stage_digest
            or prefetch.get('primary_manifest_sha256') != primary_digest
            or prefetch.get('metalink_receipt_sha256') != metalink_digest
            or prefetch.get('official') != link.get('repos') or link.get('schema') != 1
            or link.get('valid_until_unix') != prefetch.get('valid_until_unix')
            or link.get('latest') is not False):
        raise RuntimeError('AMD offline inputs are stale or unbound to the verified query')
    closure = {row['nevra']: row for row in query['plan']['closure']}
    checked = {row['nevra']: row for row in primary['packages']}
    fetched = {row['nevra']: row for row in prefetch['packages']}
    if (len(closure) != 28 or len(primary['packages']) != 28 or len(prefetch['packages']) != 28
            or closure.keys() != checked.keys() or closure.keys() != fetched.keys()):
        raise RuntimeError('AMD query, primary metadata and prefetch must cover the same 28 RPMs')
    result = []
    approved_paths = set()
    for nevra, row in closure.items():
        manifest = checked[nevra]
        repo, relative = manifest['repository'], manifest['location']
        if (repo not in AMD_DNF_MIRRORS
                or not re.fullmatch(r'Packages/[A-Za-z0-9]/[A-Za-z0-9_.+~^%-]+[.]rpm', relative)
                or not re.fullmatch(r'[0-9a-f]{64}', manifest['sha256'])
                or row['location'] != manifest['dnf_location']
                or any(row[key] != manifest[key]
                       for key in ('name', 'repository', 'download_bytes', 'installed_bytes'))):
            raise RuntimeError('AMD RPM metadata differs from the native cached solver')
        path = AMD_DNF_PAYLOAD/repo/Path(relative).name
        expected = {key: value for key, value in manifest.items() if key != 'dnf_location'}
        expected.update(file=str(path), url=AMD_DNF_MIRRORS[repo]+relative)
        observed = fetched[nevra]
        transfer = observed.get('transport', {})
        started = max(row['completed_unix'] for row in link['repos'].values())
        if ({key: value for key, value in observed.items() if key != 'transport'} != expected
                or transfer.get('size_download') != row['download_bytes']
                or type(transfer.get('size_header')) is not int or not 0 < transfer['size_header'] <= 512*1024
                or type(transfer.get('completed_unix')) is not int
                or not started <= transfer['completed_unix'] <= authenticated_at or path in approved_paths):
            raise RuntimeError('AMD prefetched RPM differs from authenticated primary metadata')
        approved_paths.add(path)
        result.append(expected)
    return {'query_sha256': query_digest, 'stage_marker_sha256': stage_digest,
            'primary_sha256': primary_digest, 'prefetch_sha256': prefetch_digest,
            'metalink_sha256': metalink_digest, 'valid_until_unix': prefetch['valid_until_unix'],
            'budget': prefetch['budget'],
            'packages': result}


def verify_amd_fresh_metalinks(receipt, proof, shared, now, *, source=AMD_DNF_METALINK.parent,
                               ttl=1800, allow_primary_updates=False):
    library = amd_offline_libraries()['CACHE']
    staged = shared/'fedora-raw-vetted-20261115a'
    owned_directory(staged, private=True)
    raw = bound_bytes(staged/'verified.json')
    if hashlib.sha256(raw).hexdigest() != proof['stage_marker_sha256']:
        raise RuntimeError('original authenticated RPM metadata staging marker changed')
    marker = json.loads(raw)
    repos = receipt['repos']
    if (repos.keys() != library['REPOS'].keys() or receipt.get('source') != marker['source']
            or receipt['valid_until_unix'] != min(row['completed_unix']+ttl
                                                for row in repos.values())):
        raise RuntimeError('AMD offline package Metalink receipt has the wrong repository set or deadline')
    for repo, row in repos.items():
        alias, key, historical_size, repomd_size, _ = library['REPOS'][repo]
        live = bound_bytes(source/(repo+'.xml'))
        for directory in (staged/key, staged/key/'repodata'):
            owned_directory(directory, private=True)
        historical = bound_bytes(staged/key/'metalink.xml')
        repomd = bound_bytes(staged/key/'repodata/repomd.xml')
        for rel, content, size in ((key+'/metalink.xml', historical, historical_size),
                                  (key+'/repodata/repomd.xml', repomd, repomd_size)):
            if marker['files'].get(rel) != {'size': size, 'sha256': hashlib.sha256(content).hexdigest()
                                           } or len(content) != size:
                raise RuntimeError('staged AMD repomd or historical Metalink changed: '+repo)
        library['verify_receipt_row'](row, live, alias, now)
        current_updates = allow_primary_updates and repo == 'updates' and row.get('source_kind') == 'primary'
        alternate = library['verify_repo_snapshot'](repo, not current_updates, live, historical, repomd, row)
        if current_updates:
            node = library['metalink_repomd'](live)
            approved = library['UPDATES_ALTERNATE']
            if (library['metalink_timestamp'](node) != approved['timestamp']
                    or library['metalink_hashes'](node) != {key: approved[key] for key in library['HASH_LENGTHS']}):
                raise RuntimeError('AMD fresh updates primary does not match the exact pinned snapshot')
        mirror = AMD_DNF_MIRRORS.get(repo)
        if (row.get('repomd_sha256') != hashlib.sha256(repomd).hexdigest()
                or row.get('repomd_size') != repomd_size
                or row.get('mirror_base') != mirror or row.get('updates_alternate') != alternate
                or row.get('source_kind') != ('alternate' if alternate else 'primary')):
            raise RuntimeError('AMD official Metalink does not prove the pinned repomd: '+repo)
        if mirror:
            ns = library['METALINK_NS']
            urls = library['one'](library['metalink_repomd'](live), ns+'resources').findall(ns+'url')
            if sum(url.get('protocol') == 'https'
                   and (url.text or '').strip() == mirror+'repodata/repomd.xml' for url in urls) != 1:
                raise RuntimeError('AMD fixed HTTPS mirror absent from the fresh official Metalink: '+repo)


def validate_amd_refresh_receipt(report, metalink, metalink_raw, history, old_packages, cutoff, now):
    bound_names = ('result.json', 'receipt.json', 'cleanup.json', 'command-result.json',
                   'native-after.json', 'amd-offline-install-before.json')
    retry = {'out': str(AMD_DNF_INSTALL_RETRY_FROM), 'status': 'FAIL', 'no_submission': True,
             'sha256': {name: AMD_DNF_INSTALL_RETRY_FAILURE_HASHES[name] for name in bound_names}}
    expiry = report.get('valid_until_unix')
    repos = metalink['repos']
    needed = AMD_DNF_INSTALL_NATIVE_TIMEOUT+AMD_DNF_INSTALL_FINISH_RESERVE
    if (report.get('schema') != 1 or report.get('status') != 'VERIFIED'
            or report.get('formal_stop') != 'NO_START' or report.get('cid') != AMD_BOOTSTRAP_CID
            or report.get('latest') is not False or metalink.get('latest') is not False
            or report.get('environment_uuid') != AMD_DNF_BINDING['environment_uuid']
            or type(report.get('generation')) is not int or report['generation'] != 1
            or report.get('prior_install_retry') != retry
            or report.get('prefetch_result_sha256') != history['prefetch_sha256']
            or report.get('historical_metalink_sha256') != history['metalink_sha256']
            or report.get('query_sha256') != history['query_sha256']
            or report.get('primary_manifest_sha256') != history['primary_sha256']
            or report.get('stage_marker_sha256') != history['stage_marker_sha256']
            or report.get('metalink_receipt_sha256') != hashlib.sha256(metalink_raw).hexdigest()
            or report.get('official') != repos or report.get('package_plan') != AMD_DNF_EXACT_PLAN
            or report.get('packages') != old_packages or report.get('rpm_download_bytes') != 0
            or report.get('package_signatures') != 'pending-native-gpg-verification'
            or metalink.get('schema') != 1 or metalink.get('valid_until_unix') != expiry
            or type(expiry) is not int or not now+needed < expiry <= now+1200
            or any(not cutoff < row['completed_unix'] <= now for row in repos.values())):
        raise RuntimeError('AMD refresh lacks current independent official authority for the same payload')
    budget, old = report['budget'], history['budget']
    body = sum(row['size_download'] for row in repos.values())
    headers = budget['metadata_refresh_response_header_bytes']
    download = 1_966_080
    write = download+2*MIB
    retry_evidence = 2*MIB
    if (budget['previous_reserved_download_bytes'] != old['reserved_download_bytes']
            or budget['previous_reserved_write_bytes'] != old['reserved_write_bytes']
            or budget['metadata_refresh_reserved_download_bytes'] != download
            or budget['metadata_refresh_reserved_write_bytes'] != write
            or budget['metadata_refresh_rpm_download_bytes'] != 0
            or budget['prior_retry_evidence_reserved_write_bytes'] != retry_evidence
            or budget['metadata_refresh_payload_bytes'] != body
            or type(headers) is not int or not 0 < headers <= download-body
            or budget['reserved_download_bytes'] != old['reserved_download_bytes']+download
            or budget['reserved_write_bytes'] != old['reserved_write_bytes']+write+retry_evidence
            or budget['future_native_payload_and_install_estimate']
                != old['future_native_payload_and_install_estimate']+write+retry_evidence
            or budget['measured_download_with_response_headers']
                != old['measured_download_with_response_headers']+body+headers
            or budget['prior_write_bytes'] != old['prior_write_bytes']
            or budget['reserved_download_bytes'] > 200*MIB or budget['reserved_write_bytes'] > 1024*MIB):
        raise RuntimeError('AMD refresh receipts do not account for the new official metadata traffic')
    return expiry


def amd_install_submission_time(attempt):
    ready = bound_read(attempt.out/'ready.json')
    before = bound_read(attempt.out/'amd-offline-install-before.json')
    needed = AMD_DNF_INSTALL_NATIVE_TIMEOUT+AMD_DNF_INSTALL_FINISH_RESERVE
    if (ready['phase_deadline']-time.monotonic() < needed
            or before['valid_until_unix']-time.time() < needed):
        raise RuntimeError('insufficient remaining AMD phase or snapshot time for formal installation')
    attempt.check_acl_watch(needed+ACL_QUEUE_RESERVE)


def amd_install_marker(profile):
    return {
        AMD_DNF_INSTALL_PROFILE: AMD_DNF_INSTALL_MARKER,
        AMD_DNF_INSTALL_RETRY_PROFILE: AMD_DNF_INSTALL_RETRY_MARKER,
        AMD_DNF_INSTALL_REFRESH_PROFILE: AMD_DNF_INSTALL_REFRESH_MARKER,
    }[profile]


def amd_install_controller(profile):
    return {
        AMD_DNF_INSTALL_PROFILE: (AMD_DNF_INSTALL_COMMAND, AMD_DNF_INSTALL_CONTROLLER_SHA256),
        AMD_DNF_INSTALL_RETRY_PROFILE: (AMD_DNF_INSTALL_RETRY_COMMAND, AMD_DNF_INSTALL_RETRY_CONTROLLER_SHA256),
        AMD_DNF_INSTALL_REFRESH_PROFILE: (AMD_DNF_INSTALL_REFRESH_COMMAND, AMD_DNF_INSTALL_REFRESH_CONTROLLER_SHA256),
    }[profile]


def validate_amd_dnf_option_failure(transaction):
    steps = transaction['native']
    expected_error = ('setopt: "package_gpgcheck=1": Option "package_gpgcheck" not found. '
                      'Add "--help" for more information about the arguments.')
    if (transaction['id'] != AMD_DNF_INSTALL_TRANSACTION or transaction['operation'] != 'install'
            or transaction['phase'] != 'failed' or transaction['failure_phase'] != 'native-command'
            or transaction['inventory_status'] != 'verified' or transaction['recovery_required'] is not False
            or len(steps) != 1 or steps[0]['state'] != 'finished' or steps[0]['exit_code'] != 2
            or steps[0]['stdout_tail'] != '' or steps[0]['stderr_tail'].splitlines()[-1:] != [expected_error]):
        raise RuntimeError('AMD retry is scoped to the exact unsupported DNF gpgcheck option')


def intel_reconciliation_sources(previous):
    names = ('inventory-before', 'cache-stat-before', 'cache-sha256', 'cache-stat-after',
             'unpack', 'finish-cached-install', 'audit', 'inventory-after')
    return [
        INTEL_REPAIR_PLAN, INTEL_RECOVER_FROM/'06-apt-package-sizes.stdout',
        *(previous/name for name in ('native-maintenance.json', 'receipt.json',
                                    'cleanup.json', 'command-result.json', 'rescue.json')),
        *(previous/f'maintenance-{index:02d}-{name}.{stream}'
          for index, name in enumerate(names) for stream in ('stdout', 'stderr')),
    ]


def repair_cache_entries(closure):
    entries = []
    for row in closure:
        name, version, arch, digest = (row[key] for key in ('name', 'version', 'arch', 'sha256'))
        size = row['download_bytes']
        if (not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+:~.-]*', version)
                or arch not in {'amd64', 'all'} or not CID.fullmatch(digest)
                or type(size) is not int or size <= 0):
            raise RuntimeError('cache entry is outside the reviewed Debian package plan')
        path = f"/var/cache/apt/archives/{name}_{version.replace(':', '%3a')}_{arch}.deb"
        entries.append({'name': name, 'version': version, 'arch': arch,
                        'path': path, 'size': size, 'sha256': digest})
    if (len(entries) != 53 or len({entry['path'] for entry in entries}) != 53
            or sum(entry['size'] for entry in entries) != 70470836):
        raise RuntimeError('cache archive count or total differs from the signed plan')
    return entries


def verify_repair_cache_report(report, expected):
    closure = report.get('verified_closure', {})
    entries = closure.get('entries')
    if (closure.get('package_count') != 53 or closure.get('total_bytes') != 70470836
            or not isinstance(entries, list) or len(entries) != 53 or len(expected) != 53):
        raise RuntimeError('native repair cache is not the whole original 53-package plan')
    for entry, planned in zip(entries, expected):
        metadata = entry.get('stat', '').split()
        if ({key: value for key, value in entry.items() if key != 'stat'} != planned
                or len(metadata) != 6 or not stat.S_ISREG(int(metadata[0], 16))
                or metadata[3] != str(planned['size'])):
            raise RuntimeError('native repair cache entry differs from its signed archive')


def same_pending_transaction_after_stop(before, after):
    """An official lifecycle stop may add only the stop timestamp to a finished step."""
    stamp = after.get('container_stopped_at')
    return (isinstance(stamp, str) and bool(stamp)
            and {key: value for key, value in before.items() if key != 'container_stopped_at'}
            == {key: value for key, value in after.items() if key != 'container_stopped_at'})


def command_timeout(attempt, requested, *, cleanup=False, holdback=0):
    limit = min(requested or 120, 120)
    if attempt.fixture.driver == 'radeon':
        watch = bound_read(attempt.out/'acl-watchdog.json')
        reserve = ACL_CLEANUP_RESERVE+holdback if cleanup else ACL_QUEUE_RESERVE
        limit = min(limit, watch['deadline']-time.monotonic()-reserve)
    if limit <= 0:
        if cleanup:
            raise RuntimeError('CLEANUP_UNKNOWN: AMD cleanup deadline has expired')
        (attempt.out/'queue-closed').touch()
        raise RuntimeError('no remaining time for a guarded native command')
    return limit


def make_service(spec):
    attempt = HardwareAttempt(Path(spec).absolute())
    if attempt.phase in BUILTIN_PHASES:
        raise RuntimeError('this phase is private to the built-in worker')
    verify_hardware_permission(attempt)
    if attempt.phase == 'native-repair':
        attempt.begin_package_maintenance()
    elif attempt.phase in {'prepare', 'gpu', 'media'}:
        attempt.require_clean_packages()
    return attempt.service()


def cuda_probe_argv(attempt):
    cuda = attempt.fixture.cuda
    probe, cubin = V100_PROBE, V100_CUBIN
    if 'tensor_sha256' in attempt.spec:
        probe, cubin = (attempt.tensor_guest_path()+'/'+name for name in V100_TENSOR_FILES)
    return [probe, cuda.uuid, cuda.pci, f'nvidia{cuda.minor}', cubin]


def run_cuda_probe(attempt):
    verify_hardware_permission(attempt)
    attempt.require_clean_packages()
    fixture = attempt.fixture
    config = attempt.active_config()
    service = attempt.service()
    binding = {'environment_id': fixture.env_id, 'environment_uuid': config['environment_uuid'],
               'generation': config['generation'], 'container_id': config['cid']}
    report = {'cid': config['cid'], 'expected': fixture.cuda.identity(), 'status': 'INVALID'}
    tensor = 'tensor_sha256' in attempt.spec
    started = copied = False

    def guest(argv):
        return service.runtime.exec(config['cid'], argv, project=str(fixture.root), env_id=fixture.env_id,
                                    workdir=None, env={}, capture=True, timeout=10)

    try:
        service.start(fixture.env_id, expected_binding=binding)
        started = True
        argv = cuda_probe_argv(attempt)
        if tensor:
            scratch = attempt.tensor_guest_path()
            absent = guest(['/bin/sh', '-c', 'test ! -e "$1" && test ! -L "$1"', 'probe', scratch])
            if absent.returncode:
                raise RuntimeError('Tensor Core guest probe path is occupied')
            source = attempt.cuda_payload('tensor')
            verify_hardware_permission(attempt)
            copied = True
            service.runtime._check_copy(source, config['cid'], scratch)
            digests = attempt.spec['tensor_sha256']
            check = guest(['/usr/bin/sha256sum', '--', argv[0], argv[-1]])
            if (check.returncode or check.stdout.splitlines() != [
                    digests[name]+'  '+scratch+'/'+name for name in V100_TENSOR_FILES]):
                raise RuntimeError('Tensor Core guest probe digest mismatch')
            report['tensor_probe_sha256'] = digests
        result = guest(argv)
        report.update({'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode:
            report['status'] = 'FAIL'
            return 1
        value = json.loads(result.stdout)
        count = 256 if tensor else 16
        expected = {'uuid': fixture.cuda.uuid, 'sm': 70, 'devices': 1,
                    'libcuda': '/run/unifier-cuda/libcuda.so.1', 'device_bytes': 40*count,
                    'integer': count, 'fp32': count, 'fp64': count, 'fp16_input_fp32_matrix': count,
                    'context_destroyed': True}
        if (any(value.get(key) != item for key, item in expected.items())
                or value.get('pci', '').lower() != fixture.cuda.pci
                or type(value.get('driver_api')) is not int or value['driver_api'] < 12090):
            raise RuntimeError('CUDA output differs from the pinned oracle')
        report['probe'] = value
        report['status'] = 'PASS'
        return 0
    except Exception as exc:
        report['error'] = str(exc)
        raise
    finally:
        try:
            try:
                if copied and guest(['/usr/bin/rm', '-rf', '--', scratch]).returncode:
                    raise RuntimeError('Tensor Core guest probe cleanup failed')
            finally:
                if started:
                    service.stop(fixture.env_id)
                    report['formal_stop'] = 'STOPPED'
        except Exception as exc:
            report.update(status='INVALID', cleanup_error=str(exc))
            raise
        finally:
            reuse.put(attempt.out/'result.json', report)


def run_vulkan_probe(attempt):
    verify_hardware_permission(attempt)
    attempt.require_clean_packages()
    fixture = attempt.fixture
    config = attempt.active_config()
    source = attempt.vulkan_binary()
    service = attempt.service()
    binding = {'environment_id': fixture.env_id, 'environment_uuid': config['environment_uuid'],
               'generation': config['generation'], 'container_id': config['cid']}
    report = {'cid': config['cid'], 'probe_sha256': attempt.spec['probe_sha256'], 'status': 'INVALID'}
    guest_path = attempt.vulkan_guest_path()
    started = copied = False

    def guest(argv, env):
        return service.runtime.exec(config['cid'], argv, project=str(fixture.root), env_id=fixture.env_id,
                                    workdir=None, env=env, capture=True, timeout=10)

    try:
        service.start(fixture.env_id, expected_binding=binding)
        started = True
        if guest(attempt.vulkan_empty_path_command(), {}).returncode != 0:
            raise RuntimeError('Vulkan probe scratch file is not new')
        service.runtime.copy_in(config['cid'], str(source), guest_path,
                                project=str(fixture.root), env_id=fixture.env_id)
        copied = True
        result = guest([guest_path], attempt.vulkan_environment())
        report.update(exit_code=result.returncode, icd_manifest=vulkan_probe.ICD)
        (attempt.out/'vulkan.stdout').write_text(result.stdout)
        (attempt.out/'vulkan.stderr').write_text(result.stderr)
        if result.returncode:
            report['status'] = 'FAIL'
            return 1
        report['probe'] = vulkan_probe.check_readback(result.stdout)
        report['status'] = 'PASS'
        return 0
    except Exception as exc:
        report['error'] = str(exc)
        raise
    finally:
        try:
            try:
                if copied:
                    if guest(['/usr/bin/rm', '--', guest_path], {}).returncode != 0:
                        raise RuntimeError('Vulkan probe scratch removal failed')
                    report['probe_removed'] = True
            finally:
                if started:
                    service.stop(fixture.env_id)
                    report['formal_stop'] = 'STOPPED'
        except Exception as exc:
            report.update(status='INVALID', cleanup_error=str(exc))
            raise
        finally:
            reuse.put(attempt.out/'result.json', report)


def escaped_argument(value):
    # An ExecStopPost property is parsed by systemd and can expand % specifiers
    # and $ variables even though no shell participates.
    if any(char in value for char in ('"', '\\', '$', '%', '\n', '\r')):
        raise ValueError('path contains unsupported systemd syntax')
    return '"'+value+'"'


def run(attempt):
    out, spec = attempt.out, attempt.spec
    if set(path.name for path in out.iterdir()) != {'spec.json'}:
        raise RuntimeError('use a new private attempt directory containing only spec.json')
    budget = attempt.monitor_budget
    cleanup = ' '.join(map(escaped_argument, (PYTHON, str(ENTRY), 'rescue', str(out/'spec.json'))))
    properties = [
        *reuse.controller_properties(budget), 'CPUQuota=100%', 'KillMode=control-group',
        'RuntimeMaxSec='+str(spec['seconds']+80), 'TimeoutStopSec=60s', 'Restart=no',
        'LimitCORE=0', 'UMask=0077', 'ExecStopPost='+cleanup]
    return subprocess.call(['systemd-run', '--user', '--wait', '--collect',
        '--unit='+spec['unit']+'-guard.service', '--setenv=GOMAXPROCS=1',
        '--setenv=LC_ALL=C.UTF-8', '--setenv=LANG=C.UTF-8',
        '--setenv=PYTHONDONTWRITEBYTECODE=1', *('--property='+item for item in properties),
        PYTHON, str(ENTRY), 'launch', str(out/'spec.json')])


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: hardware_test_guard.py run ATTEMPT/spec.json')
    mode, filename = sys.argv[1:]
    os.umask(0o077)
    env = clean_environment(os.environ)
    os.environ.clear()
    os.environ.update(env)
    attempt = HardwareAttempt(Path(filename).absolute())
    out, config = attempt.out, attempt.config
    try:
        if mode == 'run':
            return run(attempt)
        if mode == 'launch':
            attempt.prepare_base()
            return reuse.launch(config, out, hardware=attempt)
        if mode == 'worker':
            return reuse.worker(config, out, hardware=attempt)
        if mode == 'cuda-worker' and attempt.fixture.cuda and attempt.phase == 'gpu':
            if 'video_sha256' in attempt.spec:
                verify_hardware_permission(attempt)
                attempt.require_clean_packages()
                return v100_media_probe.run(attempt)
            return run_cuda_probe(attempt)
        if mode == 'vulkan-worker' and attempt.phase == 'vulkan':
            return run_vulkan_probe(attempt)
        if mode == 'media-recover-worker' and attempt.phase == 'media-recover':
            attempt.recover_intel_media()
            return 0
        if mode in {'create-worker', 'resume-worker', 'recover-worker', 'amd-bootstrap-recover-worker'}:
            if mode != attempt.phase+'-worker':
                raise RuntimeError('built-in worker mode must match the admitted phase')
            verify_hardware_permission(attempt)
            if attempt.phase == 'create':
                attempt.begin_create()
            elif attempt.phase == 'resume':
                attempt.begin_resume()
            elif attempt.phase == 'amd-bootstrap-recover':
                attempt.begin_amd_bootstrap_recover()
            else:
                attempt.begin_recover()
            return 0
        if mode == 'rescue':
            if not (out/'ready.json').exists():
                return 0
            result = attempt.cleanup(rescue=True)
            reuse.put(out/'rescue.json', result)
            return 0
        raise RuntimeError('unknown hardware guard mode')
    except Exception as exc:
        if mode in {'launch', 'rescue'}:
            detail = {'status': 'BLOCKED' if mode == 'launch' else 'CLEANUP_UNKNOWN',
                      'phase': mode, 'reason': str(exc), 'at': time.time()}
            if mode == 'launch' and not (out/'receipt.json').exists():
                reuse.put(out/'receipt.json', detail)
            if mode == 'rescue':
                reuse.put(out/'rescue.json', detail)
            if not (out/'abort.json').exists():
                reuse.put(out/'abort.json', detail)
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
