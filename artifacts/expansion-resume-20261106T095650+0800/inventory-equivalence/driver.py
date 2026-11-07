"""Stopped original Debian storage reference vs its live dpkg-query oracle."""
import hashlib,json,os,sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from tools.reuse_test_guard import make_service,get,info
from unifier.package_inventory import normalize_inventory
spec=Path(os.environ['UNIFIER_TEST_SPEC']);config=get(spec);out=spec.parent
service=make_service(spec)
result={'status':'RUNNING','scope':'one fixed stopped Debian inventory adapter; same-root peer branch is still BLOCKED'}
try:
    record=service.store.load(config['env_id']);before=info(config)['State']
    assert not before['Running']
    value=service.runtime.read_inventory_readonly(record)
    result['observation']=value.evidence()
    assert value.status=='supported',value.reason
    after=info(config)['State']
    assert after==before,'stopped observation changed execution state'
    journal=Path(config['project'])/'.lab/state/inventory-observations'/(config['cid']+'.json')
    result['storage_journal']=get(journal)
    assert result['storage_journal']['state']=='released'
    service.start(config['env_id'])
    native=service.runtime.exec(config['cid'],['dpkg-query','-W','-f','${Package}\t${Version}\n'],
        project=config['project'],env_id=config['env_id'],workdir=config['shared'],
        env={'LC_ALL':'C'},capture=True,timeout=20,user='0')
    assert native.returncode==0,native.stderr
    normalized=normalize_inventory(native.stdout)
    (out/'native-inventory.txt').write_text(normalized)
    (out/'stopped-inventory.txt').write_text(value.inventory)
    result.update(equal=normalized==value.inventory,native_exit=native.returncode,
        package_count=len(normalized.splitlines()),
        native_sha256=hashlib.sha256(normalized.encode()).hexdigest())
    assert result['equal'],'stopped database differs from native oracle'
    result['status']='PASS'
except Exception as exc:
    result.update(status='FAIL',error=str(exc),traceback=traceback.format_exc())
    raise
finally:
    if info(config)['State']['Running']:service.stop(config['env_id'])
    (out/'result.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
