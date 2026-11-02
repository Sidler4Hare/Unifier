"""Offline counterexamples: no Podman, FUSE mount or persistent user data.

Exit 1 means at least one projected result differs from the expected path
semantics. This is diagnostic evidence of an open bug, not an acceptance test.
"""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from multilinux import fuse_view
from test_projection_support import fixture


def probe():
    results = []
    with fixture() as (root, backend, server):
        for name in ('a', 'b'):
            (root / name).mkdir()
            (root / name / 'target').write_text(name)
        (root / 'a/link').symlink_to('target')
        a = server.lookup('a')
        first = server.lookup('link', a)
        (root / 'a/link').rename(root / 'b/link')
        b = server.lookup('b')
        second = server.lookup('link', b)
        reply = server.call(fuse_view.FUSE_READLINK, second)
        actual = reply[1].decode()
        expected = backend.mountpoint + '/b/target'
        results.append({'case': 'relative-symlink-after-rename',
                        'node_reused': first == second,
                        'stored_path': backend.get_node(second).path,
                        'errno': reply[2], 'actual': actual, 'expected': expected,
                        'matches_expected': reply[2] == 0 and actual == expected})
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / 'a').mkdir()
        (root / 'source').mkdir()
        (root / 'source/payload').write_bytes(b'mounted')
        mounts = [{'destination': '/a/nested', 'source': str(root / 'source'),
                   'type': 'bind', 'options': 'ro'}]
        with fixture(root, mounts=mounts) as (_root, backend, server):
            first = server.lookup('a')
            (root / 'a').rename(root / 'b')
            second = server.lookup('b')
            entries = [item['name'] for item in backend.readdir('/b')]
            results.append({'case': 'nested-mount-after-parent-rename',
                            'node_reused': first == second,
                            'stored_path': backend.get_node(second).path,
                            'actual_names': entries,
                            'unexpected_payload': backend.read('/b/nested/payload', 0, 100).decode()
                            if 'nested' in entries else None,
                            'expected': '/b must not inherit the mount configured at /a/nested',
                            'matches_expected': 'nested' not in entries})
    return {'scope': 'offline real filesystem plus protocol capture; no actual FUSE mount',
            'cases': results, 'all_match_expected': all(i['matches_expected'] for i in results)}


if __name__ == '__main__':
    result = probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result['all_match_expected'] else 1)
