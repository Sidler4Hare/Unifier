#!/usr/bin/env python3
"""Local file-view cost experiment, NOT a product or universal FUSE benchmark.

Uses existing sshfs + local sftp-server over socketpair (no network/SSH/login).
All input/output and mountpoints are newly created below .lab. No package
installation, existing containers, existing data, or global cache dropping.
Seven shuffled waves per mode; wall time excludes worker startup/mount setup.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()
FILES = 2000
MIB = 1024 * 1024


def command(argv, timeout=45):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return {'argv': argv, 'exit_code': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}


def memory(pid):
    result = {}
    try:
        for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
            if line.startswith(('Rss:', 'Pss:', 'Private_Dirty:')):
                key, value, unit = line.split()
                result[key.rstrip(':') + '_kib'] = int(value)
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        result['cpu_seconds'] = (int(stat[11]) + int(stat[12])) / os.sysconf('SC_CLK_TCK')
    except OSError as exc:
        result['error'] = str(exc)
    return result


def timed(fn):
    before = time.perf_counter_ns()
    cpu = time.process_time_ns()
    value = fn()
    return {'wall_ms': (time.perf_counter_ns()-before)/1e6,
            'worker_cpu_ms': (time.process_time_ns()-cpu)/1e6, 'value': value}


def workload(root, wave):
    expected = json.loads((root/'expected.json').read_text())
    result = {}
    def stats():
        total = sum((root/'small'/f'{i:05}.dat').stat().st_size for i in range(FILES))
        assert total == FILES*4096
        return total
    def reads():
        h=hashlib.sha256()
        for i in range(FILES):
            h.update((root/'small'/f'{i:05}.dat').read_bytes())
        assert h.hexdigest() == expected['small_sha256']
        return h.hexdigest()
    def sequential():
        h=hashlib.sha256(); total=0
        with (root/'large.dat').open('rb') as f:
            while data := f.read(MIB):
                total += len(data); h.update(data)
        assert total == 128*MIB and h.hexdigest() == expected['large_sha256']
        return {'bytes': total, 'sha256': h.hexdigest()}
    def saves():
        for i in range(20):
            output=root/'saves'/f'{wave}-{i}'
            staging=output.with_suffix('.tmp')
            with staging.open('wb') as f:
                f.write(bytes([i])*4096); f.flush(); os.fsync(f.fileno())
            os.replace(staging, output)
            assert output.read_bytes() == bytes([i])*4096
        return 20
    result['stat_2000'] = timed(stats)
    result['read_2000x4k'] = timed(reads)
    result['sequential_128m_sha256'] = timed(sequential)
    result['save_20x4k_fsync_replace'] = timed(saves)
    result['worker_memory'] = memory(os.getpid())
    return result


def registry_probe(base):
    path=base/'registry.sqlite'
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE files(path TEXT PRIMARY KEY, domain TEXT, role TEXT, handler TEXT)')
    started=time.perf_counter()
    db.executemany('INSERT INTO files VALUES(?,?,?,?)',
        ((f'/project/{i//100}/file-{i}.py',f'env-{i%3}','source','editor') for i in range(100000)))
    db.commit()
    creation_ms=(time.perf_counter()-started)*1000
    ids=random.Random(42).sample(range(100000),5000)
    def lookups():
        for i in ids:
            row=db.execute('SELECT domain,role,handler FROM files WHERE path=?',
                           (f'/project/{i//100}/file-{i}.py',)).fetchone()
            assert row==(f'env-{i%3}','source','editor')
        return len(ids)
    lookups()
    trials=[timed(lookups) for _ in range(7)]
    db.close()
    return {'rows':100000,'lookup_count_per_wave':5000,'creation_ms':creation_ms,
            'database_bytes':path.stat().st_size,'trials':trials,
            'limitations':'local cached indexed lookup only; no IPC, permissions, MIME sniffing, filesystem scan, or transaction protocol'}


class Bridge:
    def __init__(self, source, mount, strict, base):
        self.mount=mount; self.children=[]; self.logs=[]
        self.options='passive,default_permissions,nodev,nosuid'
        if strict:
            self.options += ',dir_cache=no,attr_timeout=0,entry_timeout=0,negative_timeout=0,direct_io'
        left,right=socket.socketpair()
        try:
            for name in ('sftp','sshfs'):
                self.logs.append((base/f'{mount.name}-{name}.log').open('wb'))
            self.children.append(subprocess.Popen(['/usr/libexec/openssh/sftp-server'],
                 stdin=left,stdout=left,stderr=self.logs[0]))
            self.argv=['sshfs',':'+str(source),str(mount),'-f','-o',self.options]
            self.children.append(subprocess.Popen(self.argv,stdin=right,stdout=right,stderr=self.logs[1]))
        finally:
            left.close(); right.close()
        for _ in range(100):
            if os.path.ismount(mount): break
            if self.children[-1].poll() is not None: break
            time.sleep(.05)
        if not os.path.ismount(mount):
            self.close(); raise RuntimeError('bridge did not mount')
    def metrics(self):
        return {str(p.pid):memory(p.pid) for p in self.children}
    def close(self):
        result={'mount':str(self.mount)}
        if os.path.ismount(self.mount):
            result['unmount']=command(['fusermount3','-u',str(self.mount)])
        for p in self.children:
            if p.poll() is None: p.terminate()
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: p.kill();p.wait()
        for f in self.logs: f.close()
        result['unmounted']=not os.path.ismount(self.mount)
        result['children_exited']=all(p.poll() is not None for p in self.children)
        return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path)
    parser.add_argument('--worker',type=Path)
    parser.add_argument('--wave',default='warmup')
    parser.add_argument('--bind-source',type=Path)
    args=parser.parse_args()
    if args.worker:
        if args.bind_source:
            subprocess.run(['mount','--bind',str(args.bind_source),str(args.worker)],check=True)
        print(json.dumps(workload(args.worker,args.wave)))
        return
    if not args.output or args.output.exists(): parser.error('specify a new --output file')
    signal.alarm(600)
    report={'time':dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'kernel':os.uname().release,'source_sha256':hashlib.sha256(SELF.read_bytes()).hexdigest(),
            'modes':{},'waves':[],'cleanup':[],'limitations':[
              'local SFTP over socketpair, no SSH encryption or network; not a custom FUSE implementation',
              'source page cache is warm; no cache drop, no direct disk throughput claim',
              'bind mode is a private user/mount namespace, not a full container',
              'sequential timing includes SHA256; save includes file fsync but not directory fsync',
              'seven shuffled waves; descriptive p50 and range, no reliable tail SLO',
              'user-space PSS excludes page cache, FUSE kernel memory and unrelated desktop processes']}
    report['host_before']={'meminfo':Path('/proc/meminfo').read_text(),
         'memory_psi':Path('/proc/pressure/memory').read_text(),'load':os.getloadavg(),
         'cpu':command(['lscpu','-J']),
         'versions':command(['rpm','-qf','/usr/bin/sshfs','/usr/libexec/openssh/sftp-server']),
         'filesystem':command(['stat','-f','-c','%T',str(ROOT)])}
    bridges=[]
    with tempfile.TemporaryDirectory(prefix='file-layer-probe-',dir=ROOT/'.lab') as tmp:
        base=Path(tmp); source=base/'source'; source.mkdir();(source/'small').mkdir();(source/'saves').mkdir()
        h=hashlib.sha256()
        for i in range(FILES):
            data=random.Random(i).randbytes(4096)
            (source/'small'/f'{i:05}.dat').write_bytes(data);h.update(data)
        small=h.hexdigest();h=hashlib.sha256()
        with (source/'large.dat').open('wb') as f:
            rng=random.Random(73)
            for _ in range(128):
                data=rng.randbytes(MIB);f.write(data);h.update(data)
            f.flush();os.fsync(f.fileno())
        (source/'expected.json').write_text(json.dumps({'small_sha256':small,'large_sha256':h.hexdigest()}))
        report['dataset']={'small_files':FILES,'small_bytes_each':4096,'large_bytes':128*MIB,
                           'expected':json.loads((source/'expected.json').read_text())}
        try:
            modes={'native':source,'bind_namespace':base/'bind'};modes['bind_namespace'].mkdir()
            for mode,strict in [('fuse_sftp_cached',False),('fuse_sftp_strict',True)]:
                mount=base/mode;mount.mkdir()
                bridge=Bridge(source,mount,strict,base);bridges.append(bridge);modes[mode]=mount
                report['modes'][mode]={'argv':bridge.argv,'processes':bridge.metrics()}
            report['registry']=registry_probe(base)
            def worker(mode,wave):
                argv=[sys.executable,str(SELF),'--worker',str(modes[mode]),'--wave',str(wave)]
                if mode=='bind_namespace':
                    argv=['unshare','--user','--map-root-user','--mount','--propagation','private',
                          *argv,'--bind-source',str(source)]
                r=command(argv,90)
                if r['exit_code'] != 0: raise RuntimeError(json.dumps(r))
                return json.loads(r['stdout'])
            report['warmup']={mode:worker(mode,'warm-'+mode) for mode in modes}
            rng=random.Random(20261023)
            for wave in range(7):
                order=list(modes);rng.shuffle(order)
                for mode in order:
                    metrics_before=[b.metrics() for b in bridges]
                    row={'wave':wave,'mode':mode,'order':order,
                         'measurement':worker(mode,f'{wave}-{mode}'),
                         'bridge_before':metrics_before,'bridge_after':[b.metrics() for b in bridges],
                         'host_memory_psi':Path('/proc/pressure/memory').read_text()}
                    report['waves'].append(row)
                print(f'completed wave {wave+1}/7',flush=True)
            # Tests consistency after out-of-band modifications, not performance.
            report['external_replace_visibility']={}
            for bridge in bridges:
                p=source/('visibility-'+bridge.mount.name)
                p.write_text('old'); viewed=bridge.mount/p.name
                assert viewed.read_text()=='old'
                replacement=p.with_suffix('.new');replacement.write_text('new');os.replace(replacement,p)
                report['external_replace_visibility'][bridge.mount.name]={'immediate_read':viewed.read_text(),
                    'expected':'new','note':'one immediate sample, not eventual freshness guarantee'}
            report['passed_workload_waves']=len(report['waves'])
        except Exception as exc:
            report['error']=repr(exc)
        finally:
            report['bridge_final']=[b.metrics() for b in bridges]
            report['cleanup']=[b.close() for b in bridges]
            for f in base.glob('*.log'):
                report.setdefault('logs',{})[f.name]=f.read_text()
            report['host_after']={'meminfo':Path('/proc/meminfo').read_text(),
                'memory_psi':Path('/proc/pressure/memory').read_text(),'load':os.getloadavg()}
            args.output.parent.mkdir(parents=True,exist_ok=True)
            with args.output.open('x') as f: json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    summary={}
    for mode in {r['mode'] for r in report['waves']}:
        summary[mode]={}
        for key in ('stat_2000','read_2000x4k','sequential_128m_sha256','save_20x4k_fsync_replace'):
            values=[r['measurement'][key]['wall_ms'] for r in report['waves'] if r['mode']==mode]
            summary[mode][key]={'n':len(values),'p50_ms':statistics.median(values),
                               'min_ms':min(values),'max_ms':max(values)}
    print(json.dumps({'error':report.get('error'),'summary':summary,
                     'cleanup':report['cleanup'],'visibility':report.get('external_replace_visibility')},ensure_ascii=False,indent=2))
    return int(bool(report.get('error')))


if __name__=='__main__':
    raise SystemExit(main())
