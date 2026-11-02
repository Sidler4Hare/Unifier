"""Real Btrfs/tmpfs managed-file operations and process interruption probes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.control import Control


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def worker(args):
    control = Control(args.project)
    method = control.move_file if args.mode == 'move' else control.copy_file
    if args.phase.startswith('before-'):
        wanted = args.phase.removeprefix('before-')
        transition = control.operations._transition
        def before(record, state):
            if state == wanted:
                os._exit(77)
            return transition(record, state)
        control.operations._transition = before
    def fault(point):
        if point == args.phase:
            os._exit(77)
    method(args.source_root, 'payload.bin', args.target_root, 'published.bin', fault_hook=fault)
    raise RuntimeError('requested crash boundary was not reached: ' + args.phase)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path)
    ap.add_argument('--worker', action='store_true')
    ap.add_argument('--project', type=Path)
    ap.add_argument('--source-root')
    ap.add_argument('--target-root')
    ap.add_argument('--phase')
    ap.add_argument('--mode', default='copy')
    args = ap.parse_args()
    if args.worker:
        worker(args)
        return
    assert args.output and not args.output.exists()
    sources = {str(p.relative_to(ROOT)): sha(p) for p in (ROOT / 'src/multilinux').glob('*.py')}
    report = {'passed': False, 'source_sha256': sources, 'cases': {},
              'scope': 'real Btrfs/tmpfs cross-filesystem I/O and process exit; no host power-loss claim'}
    base = ROOT / '.lab/agents/unifier-v2-stage-d2-20261025'
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = os.urandom(256 * 1024)
    data_hash = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix='native-', dir=base) as local, \
            tempfile.TemporaryDirectory(prefix='unifier-d-', dir='/dev/shm') as remote:
        local, remote = Path(local), Path(remote)
        def prepare(label, target_policy='read-write'):
            project = local / label
            project.mkdir()
            source = project / 'source'
            source.mkdir()
            target = remote / label
            target.mkdir()
            (source / 'payload.bin').write_bytes(data)
            descriptor = os.open(source / 'payload.bin', os.O_RDONLY)
            os.fsync(descriptor)
            os.close(descriptor)
            control = Control(project)
            sr = control.add_root(str(source), access_policy='read-only')
            tr = control.add_root(str(target), access_policy=target_policy)
            assert source.stat().st_dev != target.stat().st_dev
            return project, source, target, control, sr, tr

        def crash(project, sr, tr, phase, mode='copy'):
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker',
                 '--project', str(project), '--source-root', sr['root_uuid'], '--target-root', tr['root_uuid'],
                 '--phase', phase, '--mode', mode], capture_output=True, text=True, timeout=30)
            assert result.returncode == 77, (phase, result.returncode, result.stderr[-1800:])
            control = Control(project)
            records = control.list_file_operations()
            assert len(records) == 1
            return records[0]['operation_uuid']

        def verify_complete(c, sr, tr, source, target, uid, state='DONE'):
            current = c.file_operation(uid)
            assert current['state'] == state, current
            assert sha(source / 'payload.bin') == data_hash
            assert sha(target / 'published.bin') == data_hash
            assert (target / 'published.bin').stat().st_nlink == 1
            assert c.resolve_file(tr['root_uuid'], 'published.bin')['root_uuid'] == tr['root_uuid']
            assert len(c.operations.registry_collection.list()) == 1
            assert not list(target.glob('.ml-stage-*'))
            return current

        positive = [('copy', None, 'copy'), ('move-retains-source', None, 'move')]
        positive += [(phase, phase, 'copy') for phase in (
            'INTENT_DURABLE', 'STAGING', 'STAGING_CREATED', 'STAGING_READY',
            'before-TARGET_PUBLISHED', 'TARGET_PUBLISHED', 'before-REGISTRY_COMMITTED',
            'REGISTRY_COMMITTED', 'STAGE_CLEANED', 'DONE')]
        positive += [('SOURCE_CLEANUP_PENDING', 'SOURCE_CLEANUP_PENDING', 'move')]
        for index, (label, phase, mode) in enumerate(positive):
            row = {'passed': False}
            report['cases'][label] = row
            try:
                project, source, target, c, sr, tr = prepare('positive-' + str(index))
                row.update(source_dev=source.stat().st_dev, target_dev=target.stat().st_dev)
                if phase:
                    uid = crash(project, sr, tr, phase, mode)
                    row['worker_exit'] = 77
                    row['before_recovery_state'] = c.file_operation(uid)['state']
                    row['recover_first'] = c.recover_file_operations(uid)
                    row['recover_second'] = c.recover_file_operations(uid)
                else:
                    method = c.move_file if mode == 'move' else c.copy_file
                    result = method(sr['root_uuid'], 'payload.bin', tr['root_uuid'], 'published.bin')
                    uid = result['operation_uuid']
                expected = 'SOURCE_CLEANUP_PENDING' if mode == 'move' else 'DONE'
                final = verify_complete(c, sr, tr, source, target, uid, expected)
                row.update(passed=True, final_state=final['state'], source_retained=True,
                           destination_nlink=1, one_registration=True)
            except Exception as exc:
                row.update(error=str(exc), traceback=traceback.format_exc())
            print(json.dumps({'case': label, 'passed': row['passed'], 'error': row.get('error')}), flush=True)

        negative = ('occupied-target', 'readonly-target', 'unrelated-same-content-target',
                    'replaced-stage', 'changed-source', 'replaced-target-root', 'replaced-published-target')
        for index, label in enumerate(negative):
            row = {'passed': False}
            report['cases'][label] = row
            try:
                project, source, target, c, sr, tr = prepare('negative-' + str(index),
                         'read-only' if label == 'readonly-target' else 'read-write')
                if label in ('occupied-target', 'readonly-target'):
                    if label == 'occupied-target':
                        (target / 'published.bin').write_bytes(b'UNRELATED-OCCUPIED')
                    before = {p.name: sha(p) for p in target.iterdir() if p.is_file()}
                    try:
                        result = c.copy_file(sr['root_uuid'], 'payload.bin', tr['root_uuid'], 'published.bin')
                    except Exception as exc:
                        row['refusal'] = str(exc)
                    else:
                        assert result['state'] in ('CONFLICT', 'NEEDS_RECONCILIATION'), result
                    assert {p.name: sha(p) for p in target.iterdir() if p.is_file()} == before
                else:
                    phase = 'REGISTRY_COMMITTED' if label == 'replaced-published-target' else 'STAGING_READY'
                    uid = crash(project, sr, tr, phase)
                    stage = next(target.glob('.ml-stage-*'))
                    if label == 'unrelated-same-content-target':
                        (target / 'published.bin').write_bytes(data)
                    elif label == 'replaced-stage':
                        stage.rename(target / 'external-retained-stage')
                        stage.write_bytes(data)
                    elif label == 'changed-source':
                        (source / 'payload.bin').write_bytes(b'EXTERNAL-NEW-SOURCE')
                    elif label == 'replaced-target-root':
                        target.rename(target.with_name(target.name + '-parked'))
                        target.mkdir()
                    elif label == 'replaced-published-target':
                        (target / 'published.bin').unlink()
                        (target / 'published.bin').write_bytes(data)
                    original_after = (source / 'payload.bin').read_bytes()
                    target_after = {p.name: sha(p) for p in target.iterdir() if p.is_file()}
                    row['recover_first'] = c.recover_file_operations(uid)
                    row['recover_second'] = c.recover_file_operations(uid)
                    state = c.file_operation(uid)['state']
                    assert state in ('CONFLICT', 'NEEDS_RECONCILIATION'), state
                    assert (source / 'payload.bin').read_bytes() == original_after
                    assert {p.name: sha(p) for p in target.iterdir() if p.is_file()} == target_after
                    row['final_state'] = state
                assert (source / 'payload.bin').exists()
                row.update(passed=True, source_retained=True, unrelated_targets_preserved=True)
            except Exception as exc:
                row.update(error=str(exc), traceback=traceback.format_exc())
            print(json.dumps({'case': label, 'passed': row['passed'], 'error': row.get('error')}), flush=True)
    report['source_unchanged'] = all(sha(ROOT / p) == v for p, v in sources.items())
    report['passed'] = report['source_unchanged'] and all(v['passed'] for v in report['cases'].values())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'cases': len(report['cases'])}), flush=True)
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
