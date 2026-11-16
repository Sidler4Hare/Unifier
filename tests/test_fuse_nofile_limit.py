"""Exercise descriptor exhaustion in a child, never lower the suite's limits."""
import json
import resource
import subprocess
import sys
import unittest
from unittest.mock import patch
from unifier import fuse_view


class NofileLimit(unittest.TestCase):
    def test_real_emfile_then_read_same_private_tree_after_worker_adjustment(self):
        before=resource.getrlimit(resource.RLIMIT_NOFILE)
        child=r'''
import errno, json, resource, tempfile
from pathlib import Path
from unifier.fuse_view import raise_nofile_soft_limit
resource.setrlimit(resource.RLIMIT_NOFILE,(64,512))
with tempfile.TemporaryDirectory() as directory:
    paths=[]
    for i in range(320):
        path=Path(directory)/str(i//10)/str(i)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(str(i).encode())
        paths.append(path)
    held=[]
    try:
        for path in paths: held.append(path.open('rb'))
        raise AssertionError('fixture did not reach EMFILE')
    except OSError as error:
        assert error.errno==errno.EMFILE, error
    finally:
        failed_at=len(held)
        for stream in held:stream.close()
    assert raise_nofile_soft_limit()==(512,512)
    held=[]
    try:
        for path in paths:held.append(path.open('rb'))
        assert [stream.read() for stream in held]==[str(i).encode() for i in range(320)]
    finally:
        for stream in held:stream.close()
    print(json.dumps({'emfile_before':failed_at,'read_after':len(held)}))
'''
        result=subprocess.run([sys.executable,'-c',child],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        evidence=json.loads(result.stdout)
        self.assertLess(evidence['emfile_before'],64)
        self.assertEqual(evidence['read_after'],320)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE),before)

    def test_denied_raise_preserves_actual_limits(self):
        with patch.object(resource,'getrlimit',return_value=(64,512)), \
             patch.object(resource,'setrlimit',side_effect=OSError('denied')):
            self.assertEqual(fuse_view.raise_nofile_soft_limit(),(64,512))

    def test_infinite_hard_limit_uses_kernel_bound_without_raising_the_hard_limit(self):
        with patch.object(resource,'getrlimit',side_effect=[(64,resource.RLIM_INFINITY),(512,resource.RLIM_INFINITY)]), \
             patch.object(fuse_view.Path,'read_text',return_value='512\n'), \
             patch.object(resource,'setrlimit') as change:
            self.assertEqual(fuse_view.raise_nofile_soft_limit(),(512,resource.RLIM_INFINITY))
            change.assert_called_once_with(resource.RLIMIT_NOFILE,(512,resource.RLIM_INFINITY))


if __name__=='__main__':unittest.main()
