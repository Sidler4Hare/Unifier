"""Controller-owned real-mount assertions; historical probes remain unchanged."""
import errno
import importlib.util
import mmap
import os
from pathlib import Path
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('historical_extra',ROOT/'evidence/projection-retest-2026-10-27/extra_probe.py')
old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)


def historical(call, field):
    def check(f):
        data=call(f)
        assert data[field],data
        return data
    return check


def preserved_mtime_without_stat(f):
    path=f.back/'mtime-no-stat';path.write_bytes(b'A'*4096)
    fd=os.open(f.view/path.name,os.O_RDONLY)
    native=os.open(path,os.O_RDONLY)
    try:
        assert os.pread(fd,4096,0)==b'A'*4096
        before=path.stat()
        observations=[]
        for marker in (b'B',b'C',b'D',b'E'):
            with path.open('r+b') as stream:
                stream.write(marker*4096);stream.flush();os.fsync(stream.fileno())
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            expected=os.pread(native,4096,0)
            actual=os.pread(fd,4096,0)  # deliberately no explicit projected fstat
            assert actual==expected==marker*4096,{'expected':marker.decode(),'observed':actual[:1].decode()}
            observations.append(marker.decode())
        a,b=os.fstat(native),os.fstat(fd)
        assert (a.st_mtime_ns,a.st_ctime_ns,a.st_size)==(b.st_mtime_ns,b.st_ctime_ns,b.st_size)
        return dict(updates=observations,explicit_stat_before_read=False,metadata_matches_native=True)
    finally:
        os.close(native);os.close(fd)


def preserved_mtime_mmap(f):
    path=f.back/'mtime-mmap';path.write_bytes(b'A'*4096)
    fd=os.open(f.view/path.name,os.O_RDONLY)
    mapping=mmap.mmap(fd,0,access=mmap.ACCESS_READ)
    try:
        assert mapping[:]==b'A'*4096
        before=path.stat()
        for marker in (b'B',b'C',b'D'):
            with path.open('r+b') as stream:
                stream.write(marker*4096);stream.flush();os.fsync(stream.fileno())
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            projected=os.fstat(fd)
            assert mapping[:]==marker*4096,{'expected':marker.decode(),'observed':mapping[:1].decode()}
            actual=path.stat()
            assert (projected.st_mtime_ns,projected.st_ctime_ns)==(actual.st_mtime_ns,actual.st_ctime_ns)
        return dict(updates=3,mapped_pages_fresh_after_fstat=True,metadata_matches_native=True)
    finally:
        mapping.close();os.close(fd)


def symlink_component_errors(f):
    path=f.back/'link-extra';(path/'a').mkdir(parents=True);(path/'b').mkdir()
    (path/'target').write_bytes(b'ROOT');(path/'a/target').write_bytes(b'WRONG')
    (path/'a/plain').write_bytes(b'FILE');os.symlink('../b',path/'a/jump')
    raw={'chain':'../a/jump/../target','not-dir':'plain/../target','missing':'missing-prefix/../target',
         'loop':'loop','trailing-slash':'plain/','trailing-dot':'plain/.',
         'unicode 空格':'jump/../target'}
    def outcome(p):
        try:return {'bytes':p.read_bytes().decode()}
        except OSError as exc:return {'errno':exc.errno}
    rows={}
    for name,target in raw.items():
        os.symlink(target,path/'a'/name)
        native=outcome(path/'a'/name);projected=outcome(f.view/'link-extra/a'/name)
        rows[name]={'raw':target,'native':native,'projected':projected}
        assert native==projected,rows[name]
    return rows


def symlink_root_clamp(f):
    directory=f.back/'root-clamp';directory.mkdir()
    with tempfile.TemporaryDirectory(prefix='unifier-host-sentinel-') as t:
        secret=Path(t)/'private';secret.write_bytes(b'HOST-ONLY-SENTINEL')
        os.symlink('../'*128+str(secret).lstrip('/'),directory/'link')
        # Native control reaches the real host root. A projection must clamp to
        # its namespace root, where this fresh random path does not exist.
        assert (directory/'link').read_bytes()==b'HOST-ONLY-SENTINEL'
        try:
            value=(f.view/'root-clamp/link').read_bytes()
        except OSError as exc:
            assert exc.errno==errno.ENOENT,exc
            return dict(host_control_reachable=True,projected_errno=exc.errno,host_escape=False)
        raise AssertionError({'host_escape_or_wrong_target':value.decode()})


def dangling_suffix_confinement(f):
    directory=f.back/'dangling-confinement';directory.mkdir()
    with tempfile.TemporaryDirectory(prefix='unifier-dangling-sentinel-') as t:
        host=Path(t)/'private';host.write_bytes(b'HOST-ONLY-SENTINEL')
        link=directory/'link'
        os.symlink('missing/'+('../'*128)+str(host).lstrip('/'),link)
        saved=None;initial_errno=None
        try:saved=os.readlink(f.view/'dangling-confinement/link')
        except OSError as exc:
            assert exc.errno in (errno.ENOENT,errno.ENOTDIR,errno.EOPNOTSUPP),exc
            initial_errno=exc.errno
        (directory/'missing').mkdir()
        if saved is not None:
            try:data=Path(saved).read_bytes()
            except OSError as exc:assert exc.errno==errno.ENOENT,exc
            else:raise AssertionError({'saved_projected_target_escaped':data.decode(),'target':saved})
        try:data=(f.view/'dangling-confinement/link').read_bytes()
        except OSError as exc:assert exc.errno==errno.ENOENT,exc
        else:raise AssertionError({'projection_escaped_after_prefix_appeared':data.decode()})
        return dict(initial_errno=initial_errno,unresolved_suffix_then_prefix_creation_confined=True)


def inode_after_repeated_releasedir(f):
    directory=f.back/'inode-churn';directory.mkdir()
    for i in range(64):(directory/f'f{i:02d}').touch()
    os.link(directory/'f00',directory/'alias')
    before=f.status()['counters'];first=None;counts=[]
    for wave in range(5):
        with os.scandir(f.view/directory.name) as stream:
            listed={e.name:e.inode() for e in stream}
        deadline=time.monotonic()+3
        while f.status()['counters']['dir_handles']:
            assert time.monotonic()<deadline,'RELEASEDIR failed to drain'
            time.sleep(.01)
        if first is None:first=listed
        assert listed==first,{'wave':wave,'inode_mapping_changed':True}
        assert listed['alias']==listed['f00'] and len(set(listed.values()))==64
        after=f.status()['counters'];counts.append(after['nodes'])
        assert after['nodes']<=before['nodes']+3,{'before':before,'after':after}
    return dict(waves=5,entries=65,stable_mapping=True,hardlink_identity=True,
                before_nodes=before['nodes'],after_nodes=counts,resources_reclaimed=True)


CASES=[('boundary-preserved-mtime',historical(old.preserved_mtime,'held_fd_fresh_after_fstat')),
       ('boundary-symlink-dotdot',historical(old.symlink_dotdot,'same_target')),
       ('boundary-released-dirent',historical(old.released_dirent,'projected_dirent_matches_later_stat')),
       ('mtime-without-explicit-stat',preserved_mtime_without_stat),
       ('mtime-mmap-after-fstat',preserved_mtime_mmap),
       ('symlink-component-errors',symlink_component_errors),
       ('symlink-root-clamp',symlink_root_clamp),
       ('dangling-suffix-confinement',dangling_suffix_confinement),
       ('inode-releasedir-churn',inode_after_repeated_releasedir)]
watcher=old.watcher
