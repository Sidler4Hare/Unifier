"""Exercise the product operation against a real bounded private tmpfs."""
import argparse
import contextlib
import errno
import hashlib
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.control import Control


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--worker', action='store_true')
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    if not args.worker:
        result = subprocess.run(['unshare', '--user', '--map-root-user', '--mount', '--propagation', 'private',
                    sys.executable, str(Path(__file__).resolve()), '--worker', '--output', str(args.output)],
                    capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr[-2500:]
        print(result.stdout.strip())
        return
    assert os.getuid() == 0  # mapped namespace root; original host UID remains unprivileged
    (ROOT / '.lab/agents/unifier-v2-stage-d2-20261025').mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='enospc-', dir=ROOT / '.lab/agents/unifier-v2-stage-d2-20261025') as tmp:
        project = Path(tmp)
        source = project / 'source'
        target = project / 'limited-target'
        source.mkdir()
        target.mkdir()
        payload = source / 'payload.bin'
        payload.write_bytes(os.urandom(2 * 1024 * 1024))
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        baseline_dev = target.stat().st_dev
        mounted = False
        report = {'passed': False, 'source_retained': False, 'limit_bytes': 256 * 1024,
                  'scope': 'real tmpfs quota in a private user/mount namespace; not global shm exhaustion'}
        try:
            subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=256k,nosuid,nodev,noexec',
                            'unifier-d-enospc', str(target)], check=True, capture_output=True)
            mounted = True
            control = Control(project)
            sr = control.add_root(str(source), access_policy='read-only')
            tr = control.add_root(str(target))
            assert source.stat().st_dev != target.stat().st_dev
            operation = control.copy_file(sr['root_uuid'], 'payload.bin', tr['root_uuid'], 'published.bin')
            report['operation'] = operation
            assert operation['state'] == 'NEEDS_RECONCILIATION', operation
            assert '28' in json.dumps(operation) or 'No space left' in json.dumps(operation)
            assert not (target / 'published.bin').exists()
            stages = list(target.glob('.ml-stage-*'))
            assert len(stages) == 1
            report['partial_stage_bytes'] = stages[0].stat().st_size
            assert 0 < report['partial_stage_bytes'] <= report['limit_bytes']
            stage_hash = hashlib.sha256(stages[0].read_bytes()).hexdigest()
            report['recover_first'] = control.recover_file_operations(operation['operation_uuid'])
            report['recover_second'] = control.recover_file_operations(operation['operation_uuid'])
            assert hashlib.sha256(payload.read_bytes()).hexdigest() == digest
            assert hashlib.sha256(stages[0].read_bytes()).hexdigest() == stage_hash
            assert not control.operations.registry_collection.list()
            # Exercise the actual CLI handler with the isolated registered
            # roots, including its exit status after real kernel ENOSPC.
            from multilinux.cli import cmd_file_copy
            cli_args = argparse.Namespace(source_root=sr['root_uuid'], source_relative='payload.bin',
                          target_root=tr['root_uuid'], target_relative='cli-published.bin')
            captured = io.StringIO()
            with patch('multilinux.cli.control_factory', return_value=control), contextlib.redirect_stdout(captured):
                cli_exit = cmd_file_copy(cli_args)
            report['cli_exit_after_real_enospc'] = cli_exit
            report['cli_operation'] = json.loads(captured.getvalue())
            assert cli_exit != 0 and report['cli_operation']['state'] == 'NEEDS_RECONCILIATION'
            assert not (target / 'cli-published.bin').exists()
            report.update(passed=True, source_retained=True, source_sha256=digest,
                          no_published_target=True, no_registration=True, partial_stage_retained=True)
        finally:
            if mounted:
                subprocess.run(['umount', str(target)], check=True, capture_output=True)
            report['unmounted'] = target.stat().st_dev == baseline_dev
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'passed': report['passed'], 'actual_enospc': True,
                           'source_retained': report['source_retained'], 'unmounted': report['unmounted']}))


if __name__ == '__main__':
    main()
