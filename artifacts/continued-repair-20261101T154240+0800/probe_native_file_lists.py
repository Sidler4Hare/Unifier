"""Read actual Debian/Arch package lists in fresh, offline test guests."""
from pathlib import Path
import json
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from unifier.project import load_project
from unifier.runtime import PodmanRuntime, container_name
from unifier.service import EnvironmentService
BASE = Path(__file__).resolve().parent
TEST_ROOT = ROOT / '.lab/agents/continued-repair-20261101T154240+0800/file-list-root'
if TEST_ROOT.exists():
    raise RuntimeError('refusing existing test root')
TEST_ROOT.mkdir()
config, lock, _ = load_project()
runtime = PodmanRuntime()
service = EnvironmentService(TEST_ROOT, runtime, config, lock)
results = []
for target, package in [('debian13', 'base-files'), ('arch', 'filesystem')]:
    env_id = 'files-' + target
    record = service.create(target, env_id, [])
    try:
        result = service.list_package_files(env_id, package)
        paths = result['files']
        assert len(paths) > 10 and all(p.startswith('/') for p in paths), result
        results.append({'target': target, 'package': package, 'file_count': len(paths),
                        'passed': True, 'preview': paths[:5],
                        'restored_stopped': not runtime.inspect(record['runtime']['container_id']).running})
        assert results[-1]['restored_stopped']
    finally:
        info = runtime.inspect(record['runtime']['container_id'])
        assert info is None or info.container_id == record['runtime']['container_id']
        service.remove(env_id)
(BASE / 'native-file-lists.json').write_text(json.dumps({'passed': True, 'targets': results},ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False,indent=2))
