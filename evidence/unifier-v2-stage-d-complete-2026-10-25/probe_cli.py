"""Run public CLI commands in an isolated copied project, preserving main state."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--workdir', type=Path, default=HERE)
    args = ap.parse_args()
    assert not args.output.exists()
    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    project = workdir / 'cli-project'
    source = workdir / 'cli-source'
    assert not project.exists() and not source.exists()
    project.mkdir()
    for directory in ('src', 'config'):
        shutil.copytree(ROOT / directory, project / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (project / 'tools').mkdir()
    for name in ('ml.py', 'unifier.py', 'lab.py'):
        shutil.copy2(ROOT / 'tools' / name, project / 'tools' / name)
    source.mkdir()
    filename = "普通 原件';.bin"
    data = os.urandom(64 * 1024)
    (source / filename).write_bytes(data)
    report = {'passed': False, 'project': str(project), 'commands': [],
              'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (ROOT / 'src/multilinux').glob('*.py')}}
    def cli(*argv, exit=0):
        result = subprocess.run([sys.executable, str(project / 'tools/unifier.py'), *argv],
                                capture_output=True, text=True, timeout=20)
        row = {'args': list(argv), 'exit': result.returncode, 'stderr': result.stderr}
        try:
            row['json'] = json.loads(result.stdout)
        except ValueError:
            row['stdout'] = result.stdout
        report['commands'].append(row)
        assert result.returncode == exit, row
        return row.get('json')
    with tempfile.TemporaryDirectory(prefix='unifier-d-cli-', dir='/dev/shm') as remote:
        target = Path(remote)
        sr = cli('root', 'add', str(source), '--policy', 'read-only')
        tr = cli('root', 'add', str(target))
        copied = cli('file', 'copy', sr['root_uuid'], filename, tr['root_uuid'], "已 复制';.bin")
        assert copied['state'] == 'DONE' and copied['complete'] is True
        assert (target / "已 复制';.bin").read_bytes() == data
        moved = cli('file', 'move', sr['root_uuid'], filename, tr['root_uuid'], '保留源.bin', exit=1)
        assert moved['state'] == 'SOURCE_CLEANUP_PENDING' and moved['complete'] is False
        assert (target / '保留源.bin').read_bytes() == data and (source / filename).read_bytes() == data
        status = cli('file', 'status', copied['operation_uuid'])
        assert status['state'] == 'DONE'
        cli('file', 'recover', copied['operation_uuid'])
        cli('file', 'recover', copied['operation_uuid'])
        cli('file', 'copy', sr['root_uuid'], filename, tr['root_uuid'], "已 复制';.bin", exit=1)
        assert (target / "已 复制';.bin").read_bytes() == data
        cli('file', 'resolve', tr['root_uuid'], "已 复制';.bin")
        report.update(passed=True, source_retained=True, completed_copy_exit=0,
                      incomplete_move_exit=1, existing_target_refused=True,
                      whitespace_quotes_semicolon_preserved=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'commands': len(report['commands']),
                      'copy_exit': 0, 'partial_move_exit': 1}))


if __name__ == '__main__':
    main()
