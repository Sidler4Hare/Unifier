"""Run C's final offline/native checks against one unchanged source snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=HERE)
    ap.add_argument('--prefix', required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    output = args.out / (args.prefix + '-gate.json')
    assert not output.exists()
    sources = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in (ROOT / 'src/multilinux').glob('*.py')}
    report = {'started_at': time.time(), 'source_sha256': sources, 'steps': {}, 'passed': False}
    try:
        unit_path = args.out / (args.prefix + '-regression.txt')
        with unit_path.open('x') as log:
            r = subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        report['unit_log'] = str(unit_path)
        report['steps']['unit_exit'] = r.returncode
        assert r.returncode == 0
        for script, label in [('probe_live.py', 'matrix'), ('probe_lifecycle.py', 'lifecycle'),
                              ('probe_missing_backend.py', 'missing-backend')]:
            result_path = args.out / (args.prefix + '-' + label + '.json')
            r = subprocess.run(['python3', str(HERE / script), '--output', str(result_path)],
                                cwd=ROOT, capture_output=True, text=True)
            report['steps'][label] = {'path': str(result_path), 'exit': r.returncode,
                                      'stdout': r.stdout.strip(), 'stderr': r.stderr[-3000:]}
            print(json.dumps({'step': label, 'exit': r.returncode, 'result': r.stdout.strip()}), flush=True)
            assert r.returncode == 0
        report['source_unchanged'] = all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == s
                                          for p, s in sources.items())
        assert report['source_unchanged']
        report['passed'] = True
    except Exception as exc:
        report['error'] = str(exc)
    report['finished_at'] = time.time()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'output': str(output)}), flush=True)
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
