"""Native-file installs, literal filenames, and pre-mutation target refusal."""
import json,hashlib,shutil,subprocess,os
from pathlib import Path
import primary_live as core
p=core.Probe('native-package-attempt2',core.LAB/'management');source=core.LAB/'environments';manifest=json.loads((core.EVID/'native-jq-files/manifest.json').read_text())
p.js('debian-record-maintenance',['pkg','install','debian13','libasound2t64','--use-command=/usr/bin/test','--use-command=-r','--use-command=/usr/lib/x86_64-linux-gnu/libasound.so.2'],root=source,timeout=900)
p.js('arch-less-serialized',['pkg','install','arch','less'],root=source,timeout=900)
for row in sorted(manifest,key=lambda r:r['target']=='debian13'):
 t=row['target'];suffix='.deb' if t=='debian13' else '.pkg.tar.zst';path=core.EVID/'native-jq-files'/('中文 空格 $; jq'+suffix);shutil.copyfile(row['path'],path)
 assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
 out=p.js(t+'-local-install',['install','--target',t,'--exec','/usr/bin/jq','--app','jq','--package','jq','--json',path],timeout=900)
 p.note(t+'-local-transaction',ok=out['transaction']['phase']=='completed',version=row['version'],sha256=row['sha256'],app_id=out['app_id'],transaction=out['transaction']['id'])
 result=p.call(t+'-local-oracle',['run','--target',t,'jq','--','-r','map(.n)|add',core.LAB/'shared-project/numbers.json'])
 p.note(t+'-local-result',ok=result.stdout=='5\n')
# Target rejection is a negative policy case, counted separately from workflows.
deb=next(r for r in manifest if r['target']=='debian13')['path']
registry=source/'.lab/state/transactions';before={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in registry.glob('*.json')}
p.call('wrong-target-before-mutation',['install','--target','arch','--exec','/usr/bin/jq','--app','jq','--package','jq','--json',deb],expect=1)
after={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in registry.glob('*.json')};p.note('wrong-target-no-transaction',ok=before==after,status='PASS_EXPECTED_REJECTION')
p.call('unknown-alpine-target',['compat','check','--target','alpine'],expect=1);p.note('apk-boundary',ok=True,status='NOT_IMPLEMENTED_EXPECTED_REJECTION')
p.note('complete',ok=True)
