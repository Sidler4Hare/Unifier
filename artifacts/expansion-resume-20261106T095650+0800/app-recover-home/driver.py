import contextlib,io,json,sys,shutil,os
from pathlib import Path
root=Path(__file__).resolve().parents[3];sys.path[:0]=[str(root/'src'),str(root)]
from unifier import cli
from unifier.runtime import PodmanRuntime,_run_podman
from unifier.project import load_project
from unifier.service import EnvironmentService
out=Path(__file__).resolve().parent;config=json.loads((out.parent/'live-minimum/spec.json').read_text());project=Path(config['project'])
lease=project/'.lab/leases'/(config['env_id']+'.json')
backup=root/'.lab/agents/expansion-resume-20261106T095650+0800/before-app-recover-home';backup.mkdir(mode=0o700);shutil.copy2(lease,backup/'debian13.json')
calls=[]
def readonly(argv,**kwargs):
 assert argv[0]=='podman' and argv[1] in ('inspect','info'),argv
 if argv[1]=='inspect':assert argv[-1] in (config['cid'],config['container_name']),argv
 calls.append(argv[:2]);return _run_podman(argv,**kwargs)
rt=PodmanRuntime(runner=readonly);before=rt.inspect(config['cid']);assert before is not None and not before.running and before.container_id==config['cid']
targets,_,_=load_project();lock=json.loads(Path(config['image_lock']).read_text());svc=EnvironmentService(project,rt,targets,lock)
record=svc.store.load(config['env_id']);assert record['environment_uuid']==config['environment_uuid'] and record['generation']==config['generation']
cli.service_factory=lambda:svc;capture=io.StringIO()
with contextlib.redirect_stdout(capture):code=cli.main(['app','recover',config['env_id']])
assert code==0,capture.getvalue()
value=json.loads(lease.read_text());assert not value['leases'],value
after=rt.inspect(config['cid']);assert not after.running
result={'status':'PASS','scope':'formal app recover on exact stopped CID; start/exec/stop mutations forbidden by runner','recovery':json.loads(capture.getvalue()),'after_leases':value,'runtime_calls':calls}
(out/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
