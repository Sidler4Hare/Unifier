"""Actual source bundles, private launcher switching, no containers or user install."""
import hashlib,json,os,subprocess,sys,tarfile
from pathlib import Path
repo=Path(__file__).resolve().parents[2]
out=Path(__file__).resolve().parent
private=Path(os.environ['TMPDIR'])/'artifact-smoke'
private.mkdir(mode=0o700)
env=dict(os.environ);env.pop('PYTHONPATH',None);env['PYTHON']='/usr/bin/python3'
env['UNIFIER_HOME']=str(private/'userdata')
data=Path(env['UNIFIER_HOME']);data.mkdir();marker=data/'preserved-document.txt';marker.write_text('preserved before upgrade')
def call(argv,cwd=None):
    return subprocess.check_output([str(x) for x in argv],cwd=cwd or private,env=env,stderr=subprocess.STDOUT,text=True,timeout=20).strip()
def unpack(version):
    path=repo/'artifacts'/('unifier-'+version+'.tar.gz')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest==Path(str(path)+'.sha256').read_text().split()[0]
    base=private/('source-'+version);base.mkdir()
    with tarfile.open(path) as archive:
        members=archive.getmembers()
        assert len(members)<1000 and sum(x.size for x in members)<64*1024**2
        assert all(not Path(x.name).is_absolute() and '..' not in Path(x.name).parts for x in members)
        archive.extractall(base,filter='data')
    source=base/('unifier-'+version)
    manifest=json.loads((source/'BUNDLE-MANIFEST.json').read_text())
    assert manifest['version']==version
    for name,record in manifest['files'].items():
        p=source/name
        if record['kind']=='file':assert not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==record['sha256'],name
        else:assert p.is_symlink() and os.readlink(p)==record['target'],name
    return source,digest,len(manifest['files'])
old,old_digest,_=unpack('0.0.2');new,new_digest,entries=unpack('0.0.3')
assert (new/'AGENTS.md').is_file() and (new/'docs/v0.0.3.md').is_file()
bindir=private/'bin'
call(['/usr/bin/python3',old/'tools/install_unifier.py','--bin-dir',bindir])
launcher=bindir/'unifier';old_version=call([launcher,'--version']);assert '0.0.2' in old_version
call(['/usr/bin/python3',new/'tools/install_unifier.py','--bin-dir',bindir,'--upgrade'])
new_version=call([launcher,'--version']);assert '0.0.3' in new_version
assert launcher.resolve()==new/'bin/unifier'
log=private/'missing.log';log.write_text('vendor: error while loading shared libraries: libasound.so.2: cannot open shared object file: No such file or directory\n')
diagnosis=json.loads(call([launcher,'pkg','repair-deps','unregistered','--from-log',log,'--diagnose-only','--json']))
assert diagnosis['libraries']==['libasound.so.2'] and diagnosis['library_repair_candidate']
assert diagnosis['provider_resolution']=='not-run' and diagnosis['workflow']=='unverified'
assert marker.read_text()=='preserved before upgrade'
rollback,=bindir.glob('*.rollback');os.replace(rollback,launcher)
assert launcher.resolve()==old/'bin/unifier'
assert '0.0.2' in call([launcher,'--version']) and marker.read_text()=='preserved before upgrade'
result={'status':'PASS','scope':'private artifact install/upgrade/diagnose/rollback only; no containers or daily deployment',
'old_version':old_version,'new_version':new_version,'old_sha256':old_digest,'new_sha256':new_digest,'manifest_entries':entries,
'private_data_preserved':True,'rollback_verified':True,'diagnosis':diagnosis}
(out/'artifact-smoke-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
