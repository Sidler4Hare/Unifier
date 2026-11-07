"""Read-only tool/provider preparation plan on the original Debian instance."""
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from tools.reuse_test_guard import make_service,get,info

spec=Path(os.environ['UNIFIER_TEST_SPEC']);config=get(spec);out=spec.parent
service=make_service(spec)
result={'status':'RUNNING','scope':'read-only guest toolchain/repository plan; no package change'}
def guest(argv,accepted=(0,),seconds=25):
    value=service.runtime.exec(config['cid'],argv,project=config['project'],env_id=config['env_id'],
        workdir=config['shared'],env={'LC_ALL':'C'},capture=True,timeout=seconds,user='0')
    if value.returncode not in accepted:raise RuntimeError(str(argv[:3])+': '+value.stderr[-1000:])
    return value
try:
    service.start(config['env_id'])
    result['tools']=guest(['/bin/sh','-c',
        'for tool in gcc cc readelf dpkg-deb apt-file; do command -v "$tool" || :; done']).stdout
    packages=['gcc','binutils','dpkg-dev','apt-file','libestr0','libestr-dev',
              'libfastjson4','libfastjson-dev','libconfig9','libconfig-dev','libcapstone5','libcapstone-dev']
    installed=guest(['dpkg-query','-W','-f','${Package}\t${Version}\t${db:Status-Abbrev}\n',*packages],accepted=(0,1))
    result['installed']=installed.stdout;result['installed_query_exit']=installed.returncode
    result['cached_packages']={}
    for package in packages:
        value=guest(['apt-cache','show','--no-all-versions',package],accepted=(0,100))
        fields={}
        for line in value.stdout.splitlines():
            key,sep,text=line.partition(': ')
            if sep and key in ('Package','Version','Size','Installed-Size','SHA256','Filename'):
                fields[key]=text
        result['cached_packages'][package]={'exit_code':value.returncode,**fields}
    simulated=guest(['apt-get','-s','install','gcc','binutils','dpkg-dev','apt-file'],accepted=(0,100))
    (out/'toolchain-simulation.log').write_text(simulated.stdout+simulated.stderr)
    result['simulation_exit']=simulated.returncode
    pending=[];download=installed_kib=0
    for line in simulated.stdout.splitlines():
        match=re.match(r'^Inst ([A-Za-z0-9+.:_-]+)(?: \[[^\]]+\])? \(([^ ]+)',line)
        if not match:continue
        package,version=match.groups()
        metadata=guest(['apt-cache','show','--no-all-versions',package+'='+version]).stdout
        fields=dict(line.split(': ',1) for line in metadata.splitlines() if ': ' in line)
        size=int(fields['Size']);write=int(fields['Installed-Size'])
        pending.append({'package':package,'version':version,'download_bytes':size,'installed_kib':write})
        download+=size;installed_kib+=write
    result.update(pending_toolchain=pending,toolchain_download_bytes=download,
                  toolchain_installed_bytes=installed_kib*1024,status='PASS' if simulated.returncode==0 else 'BLOCKED_CACHE')
    result['disk']=guest(['df','-B1','/','/tmp',config['shared']]).stdout
finally:
    service.stop(config['env_id'])
assert not info(config)['State']['Running']
(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({k:result[k] for k in ('status','tools','installed','toolchain_download_bytes','toolchain_installed_bytes')},ensure_ascii=False))
