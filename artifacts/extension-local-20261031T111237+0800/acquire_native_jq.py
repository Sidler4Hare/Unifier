from pathlib import Path
import subprocess,json,hashlib,os,tarfile,io
REPO=Path(__file__).resolve().parents[2];EVID=Path(__file__).resolve().parent;LAB=REPO/'.lab/extension-local-20261031';out=EVID/'native-jq-files';out.mkdir(exist_ok=False)
fixture=json.loads((EVID/'fixture.json').read_text());records=fixture['records'];rows=[]
def run(name,args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=180);(out/(name+'.stdout')).write_text(p.stdout);(out/(name+'.stderr')).write_text(p.stderr)
 if p.returncode:raise RuntimeError(name+': '+p.stderr[-1000:])
 return p.stdout.strip()
cid=records['debian13']['runtime']['container_id'];folder=LAB/'management/containers/debian13/files/native-packages';folder.mkdir(exist_ok=True)
v=run('debian-version',['podman','exec',cid,'dpkg-query','-W','-f=${Version}','jq'])
uri=run('debian-origin',['podman','exec','--user','1000:1000','--workdir',str(folder),cid,'apt-get','--print-uris','download','jq='+v])
run('debian-download',['podman','exec','--user','1000:1000','--workdir',str(folder),cid,'apt-get','download','jq='+v])
files=list(folder.glob('jq_*.deb'));assert len(files)==1,files
p=files[0];rows.append({'target':'debian13','package':'jq','version':v,'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size,'origin':uri,'acquisition':'ordinary-user apt-get download of exact installed version; authenticated repository metadata'})
cid=records['arch']['runtime']['container_id'];version=run('arch-version',['podman','exec',cid,'pacman','-Q','jq']).split()[1]
paths=run('arch-cache',['podman','exec',cid,'find','/var/cache/pacman/pkg','-maxdepth','1','-type','f','-name','jq-*.pkg.tar.*']).splitlines();matches=[]
for remote in paths:
 if remote.endswith('.sig'):continue
 p=out/Path(remote).name
 subprocess.run(['podman','cp',cid+':'+remote,str(p)],capture_output=True,text=True,check=True,timeout=60)
 with tarfile.open(p) as t:info=t.extractfile('.PKGINFO').read().decode()
 if ('pkgver = '+version+'\n') in info:matches.append((p,info,remote))
assert len(matches)==1,matches
p,info,remote=matches[0];origin=run('arch-origin',['podman','exec',cid,'pacman','-Sp','--print-format','%l','jq'])
rows.append({'target':'arch','package':'jq','version':version,'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size,'origin':origin,'cached_origin':remote,'pkginfo':info,'acquisition':'native package cache from earlier signed full-system repository transaction; copied exact CID file, metadata version verified'})
(out/'manifest.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n');print(json.dumps(rows,ensure_ascii=False,indent=2),flush=True)
