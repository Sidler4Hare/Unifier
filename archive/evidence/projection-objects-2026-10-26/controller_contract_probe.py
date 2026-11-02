#!/usr/bin/env python3
"""Additional controller counterexamples for aggregate bounds and topology."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from multilinux.view_backend import IdMap, ViewBackend, ViewError


@contextlib.contextmanager
def fixture(mounts=None, **limits):
    with tempfile.TemporaryDirectory(prefix='projection-contract-') as d:
        root = Path(d)
        back = root / 'merged'
        back.mkdir()
        fd = os.open(back, os.O_PATH | os.O_DIRECTORY)
        backend = None
        try:
            backend = ViewBackend(mountpoint=str(root/'view'),merged_fd=fd,
                merged_stat=os.fstat(fd),mounts=mounts(root) if mounts else [],capabilities=[],
                id_map=IdMap(os.getuid(),os.getgid(),0,0,0,0,0,0,'default'),
                view_uuid='controller-contract',**limits)
            yield root, back, backend
        finally:
            if backend:
                backend.close()
            os.close(fd)


def aggregate_budget():
    with fixture(max_dir_entries=5) as (root,back,b):
        (back/'one').touch()
        a=b.open_dir(1)
        try:
            try:
                c=b.open_dir(1)
            except ViewError as exc:
                assert exc.errno == 28, exc
            else:
                b.release_handle(c)
                raise AssertionError('two 3-entry streams exceeded global 5-entry budget')
        finally:
            b.release_handle(a)
        return b.stats()


def dirent_identity():
    with fixture() as (root,back,b):
        (back/'file').touch()
        node=b.lookup_node(1,'file')
        fh=b.open_dir(1)
        try:
            entries=b.readdir_handle(fh,0)[1]
            item=next(e for e in entries if e['name']=='file')
            assert item['ino']==b.node_attr(node)['ino'], (item['ino'],node.nodeid)
        finally:
            b.release_handle(fh)
        return {'same_ino':True}


def path_api_reclaims():
    with fixture() as (root,back,b):
        before=b.stats()['nodes']
        for i in range(100):
            p=back/str(i)
            p.touch()
            b.getattr('/'+str(i))
            p.unlink()
        after=b.stats()['nodes']
        assert after<=before+2, (before,after)
        return {'before':before,'after':after}


def mounts(root):
    outer=root/'outer';inner=root/'inner'
    outer.mkdir();inner.mkdir()
    under=root/'merged/outer/deep';under.mkdir(parents=True)
    (under/'underlay-secret').write_text('must remain masked')
    return [dict(destination='/outer',source=str(outer)),
            dict(destination='/outer/deep/child',source=str(inner))]


def no_underlay_fallback():
    with fixture(mounts) as (root,back,b):
        names=[e['name'] for e in b.readdir('/outer/deep')]
        assert 'child' in names and 'underlay-secret' not in names,names
        return {'names':names}


def unavailable_listing():
    def spec(root):
        source=root/'source';source.mkdir()
        return [dict(destination='/mount',source=str(source))]
    with fixture(spec) as (root,back,b):
        (root/'source').rename(root/'replaced')
        (root/'source').mkdir()
        try:
            b.readdir('/')
        except ViewError as exc:
            assert exc.errno==5,exc
            return {'failed_closed':True}
        raise AssertionError('directory listed an invalid provider without checking identity')


def failed_path_reclaims():
    with fixture(max_nodes=4) as (root,back,b):
        for i in range(25):
            (back/str(i)).mkdir()
            try:
                b.getattr('/'+str(i)+'/missing')
            except ViewError as exc:
                assert exc.errno==2, (i,exc.errno)
            else:
                raise AssertionError('missing child unexpectedly existed')
            assert b.stats()['nodes']==1, b.stats()
        return {'nodes_after_failed_paths':b.stats()['nodes']}


def source_open_exhaustion_errno():
    def spec(root):
        source=root/'source';source.mkdir()
        (source/'file').write_bytes(b'pinned')
        return [dict(destination='/mount',source=str(source))]
    with fixture(spec) as (root,back,b):
        mount=b.lookup_node(1,'mount')
        child=b.lookup_node(mount.nodeid,'file')
        fh=b.open_node(child.nodeid,os.O_RDONLY)
        original=os.open
        def limited(path,*args,**kwargs):
            if str(path)==str(root/'source'):
                raise OSError(24,'injected source verification FD exhaustion')
            return original(path,*args,**kwargs)
        try:
            with patch('multilinux.projection.os.open',side_effect=limited):
                try:
                    b.lookup_node(1,'mount')
                except ViewError as exc:
                    assert exc.errno==24,exc.errno
                else:
                    raise AssertionError('source open exhaustion was ignored')
                assert b.read_handle(fh,0,100)==b'pinned'
        finally:
            b.release_handle(fh)
        return {'emfile_preserved':True,'held_read_works':True}


def run():
    result={}
    for call in (aggregate_budget,dirent_identity,path_api_reclaims,no_underlay_fallback,unavailable_listing,
                 failed_path_reclaims,source_open_exhaustion_errno):
        try:
            result[call.__name__]={'passed':True,'data':call()}
        except Exception as exc:
            result[call.__name__]={'passed':False,'error':repr(exc)}
    print(json.dumps(result,indent=2))
    return 0 if all(r['passed'] for r in result.values()) else 1


if __name__=='__main__':
    raise SystemExit(run())
