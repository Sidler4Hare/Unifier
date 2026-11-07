import json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parents[3];sys.path[:0]=[str(root/'src'),str(root)]
from tools.reuse_test_guard import make_service,get,info,capture_binding
spec=Path(os.environ['UNIFIER_TEST_SPEC']);config=get(spec);out=spec.parent
service=make_service(spec);before=info(config)
assert not before['State']['Running']
try:
 service.start(config['env_id'])
 binding=capture_binding(config,out);assert binding['coverage_verified']
 native=service.runtime.exec(config['cid'],['/usr/bin/python3','-c','import json,os;print(json.dumps({"uid":os.getuid(),"pid":os.getpid(),"pid_namespace":os.readlink("/proc/self/ns/pid")}))'],project=config['project'],env_id=config['env_id'],workdir=config['shared'],env={},capture=True,timeout=10,user='1000:1000')
 assert native.returncode==0,native.stderr
 result={'status':'PASS','scope':'fixed Debian minimum start/exec/stop and actual aggregate coverage only','native':json.loads(native.stdout),'binding':binding}
finally:
 service.stop(config['env_id'])
after=info(config);assert not after['State']['Running'];result['after']=after['State']
(out/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
