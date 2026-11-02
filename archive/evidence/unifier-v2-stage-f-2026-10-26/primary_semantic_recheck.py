"""Real fresh-view contract recheck; no throughput rerun or product edits."""
import json
import os
from pathlib import Path
import sys
import time

E = Path(__file__).resolve().parent
sys.path.insert(0, str(E/'implementation'))
import common
from probe_projection import semantic_tests, atomic_replace, verify_view_gone

out = E/'semantic-recheck-1.json'
if out.exists():
    raise SystemExit('refusing overwrite')
fixtures = common.load_fixtures(E/'fixtures.json')
facade = common.product(fixtures)
common.check_fixture_state(facade['service'], fixtures, require_stopped=True)
result = {'scope': 'Three fresh real product projections; corrected projected-link contract; original formal failure retained', 'targets': {}}
for target in common.TARGETS:
    item = fixtures['targets'][target]
    workspace = Path(item['workspace'])
    dataset = workspace/'dataset'
    view = facade['views'].open(item['environment']['id'])
    try:
        mount = Path(view['mountpoint'])
        sem = semantic_tests(facade, fixtures, target, view, mount, workspace, dataset, item)
        private = dataset/('controller-semantic-'+str(time.time_ns()))
        private.mkdir()
        projected = mount/private.relative_to('/')
        path = private/'unread-fd.bin'
        old = b'A'*8192
        new = b'B'*8192
        path.write_bytes(old)
        host_fd = os.open(path, os.O_RDONLY)
        view_fd = os.open(projected/path.name, os.O_RDONLY)
        try:
            atomic_replace(path, new)
            host_read = os.pread(host_fd, len(old), 0)
            view_read = os.pread(view_fd, len(old), 0)
            sem['unread_fd_control'] = {
                'native_preserves_old_inode': host_read == old,
                'projection_preserves_old_inode': view_read == old,
                'projection_reads_replacement': view_read == new,
                'projection_sha256': common.sha256_bytes(view_read),
                'old_sha256': common.sha256_bytes(old),
                'new_sha256': common.sha256_bytes(new),
            }
        finally:
            os.close(host_fd)
            os.close(view_fd)
        changing = private/'size-change.bin'
        changing.write_bytes(b'C'*32)
        old_observed = (projected/changing.name).read_bytes()
        replacement = b'D'*128
        atomic_replace(changing, replacement)
        immediate = (projected/changing.name).read_bytes()
        time.sleep(1.3)
        delayed = (projected/changing.name).read_bytes()
        sem['size_change'] = {
            'old_bytes': len(old_observed), 'expected_new_bytes': len(replacement),
            'immediate_bytes': len(immediate), 'delayed_bytes': len(delayed),
            'immediate_matches_old': immediate == old_observed,
            'immediate_matches_new': immediate == replacement,
            'immediate_matches_neither_version': immediate not in (old_observed, replacement),
            'delayed_matches_new': delayed == replacement,
        }
        sem['checks']['native_unread_fd_control'] = host_read == old
        sem['checks']['size_change_eventual_freshness'] = delayed == replacement
        result['targets'][target] = sem
    finally:
        facade['views'].revoke(view['view_uuid'])
        cleanup = verify_view_gone(facade, view)
        if target in result['targets']:
            result['targets'][target]['cleanup'] = cleanup
        assert all(cleanup[n] is True for n in ('registry_empty','not_mounted','worker_gone','broker_gone','launcher_gone','no_process_references')), cleanup
result['passed'] = len(result['targets']) == 3 and all(all(r['checks'].values()) for r in result['targets'].values())
common.write_new_json(out, result)
print(json.dumps({'passed': result['passed'], 'targets': {t:{'checks':r['checks'],'unread_fd':r['unread_fd_control'],'size_change':r['size_change']} for t,r in result['targets'].items()}}, ensure_ascii=False))
raise SystemExit(0 if result['passed'] else 1)
