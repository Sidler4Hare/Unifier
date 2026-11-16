"""Private paths and bounded resource diagnostics shared by test supervisors."""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import tempfile


SESSION_VARIABLES = ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS',
                     'SESSION_MANAGER', 'XAUTHORITY', 'SSH_AUTH_SOCK')
EXTRA_VARIABLES = {'LC_ALL', 'LANG', 'PYTHONUNBUFFERED', 'PATH'}


def result_exit_code(reason, command_result):
    """Keep a failed command distinct from an invalid/protected attempt."""
    if reason or not isinstance(command_result, dict) or type(command_result.get('exit_code')) is not int:
        return 2
    return 0 if command_result['exit_code'] == 0 else 1


def parse_pressure(text):
    result = {}
    for line in text.splitlines():
        words = line.split()
        if not words or words[0] not in {'some', 'full'} or words[0] in result:
            raise ValueError('invalid or duplicate PSI line')
        fields = {}
        for word in words[1:]:
            key, value = word.split('=', 1)
            if key in fields:
                raise ValueError('duplicate PSI field')
            fields[key] = value
        values = {}
        if not {'avg10', 'avg60', 'avg300', 'total'} <= fields.keys():
            raise ValueError('missing PSI field')
        for key in ('avg10', 'avg60', 'avg300'):
            value = float(fields[key])
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError('invalid PSI percentage')
            values[key] = value
        total = fields['total']
        if not total.isdecimal():
            raise ValueError('invalid PSI total')
        values['total'] = int(total)
        result[words[0]] = values
    if set(result) != {'some', 'full'}:
        raise ValueError('missing PSI measurement')
    return result


def _read(path):
    with Path(path).open() as stream:
        text = stream.read(65537)
    if len(text) > 65536:
        raise ValueError('resource metric exceeds read budget')
    return text


def cgroup_diagnostics(path):
    path = Path(path)
    pressure = parse_pressure(_read(path / 'memory.pressure'))
    swap = _read(path / 'memory.swap.current').strip()
    if not swap.isdecimal():
        raise ValueError('invalid cgroup swap measurement')
    values = {}
    for line in _read(path / 'memory.stat').splitlines():
        key, value = line.split()
        if not value.isdecimal() or key in values:
            raise ValueError('invalid memory.stat measurement')
        values[key] = int(value)
    if not {'anon', 'file'} <= values.keys():
        raise ValueError('missing anonymous/file memory measurement')
    keys = {'anon', 'file', 'pgscan', 'pgsteal', 'pgfault', 'pgmajfault',
            'workingset_refault_anon', 'workingset_refault_file',
            'workingset_activate_anon', 'workingset_activate_file'}
    return {'memory_pressure': pressure, 'swap_current': int(swap),
            'memory_stat': {key: value for key, value in values.items() if key in keys}}


@contextmanager
def short_temporary_directory():
    # tempfile's default inherits TMPDIR, which may already exceed AF_UNIX limits.
    with tempfile.TemporaryDirectory(prefix='ut-', dir='/tmp') as temporary:
        yield Path(temporary)


def offline_environment(root, private, temporary, extra=None):
    extra = {} if extra is None else extra
    if (not isinstance(extra, dict) or set(extra) - EXTRA_VARIABLES
            or any(not isinstance(v, str) or '\0' in v for v in extra.values())):
        raise ValueError('offline environment override changes isolation')
    root, private, temporary = Path(root), Path(private), Path(temporary)
    env = {key: value for key, value in os.environ.items() if key not in SESSION_VARIABLES}
    names = {'HOME': 'home', 'XDG_CONFIG_HOME': 'config', 'XDG_CACHE_HOME': 'cache',
             'XDG_DATA_HOME': 'data', 'UNIFIER_HOME': 'installation'}
    for key, name in names.items():
        directory = private / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(directory)
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = temporary / 'r'
    runtime.mkdir(mode=0o700, exist_ok=True)
    env['XDG_RUNTIME_DIR'] = str(runtime)
    env.update(TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE='1',
               MULTILINUX_INTEGRATION='0', MULTILINUX_EVIDENCE='0',
               LC_ALL='C', LANG='C', GOMAXPROCS='1',
               PYTHONPATH=os.pathsep.join(str(root / p) for p in ('src', 'tests', '.')))
    env.update(extra)
    return env
