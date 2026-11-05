import os,subprocess,shutil,tarfile,json,hashlib
from pathlib import Path
P=Path(__file__).resolve().parent;R=P.parents[2];CID='8399ccd38b137d9283b6587007a43744e8d334809ed4864f002f091009ca555e';os.environ['GOMAXPROCS']='2'
common=P/'common';(common/'fonts').mkdir(parents=True,exist_ok=True)
for f in [Path('/usr/share/fonts/google-noto/NotoSans-Regular.ttf'),Path('/usr/share/fonts/google-noto-sans-cjk-vf-fonts/NotoSansCJK-VF.ttc')]:shutil.copy2(f,common/'fonts'/f.name)
for source,dest in [('中文 文档.docx','writer.docx'),('中文 表格.xlsx','sheet.xlsx'),('中文 演示.pptx','slides.pptx')]:shutil.copy2(R/'.lab/extension-local-20261031/shared-project/office-fixtures'/source,common/dest)
shutil.copy2(P/'Xauthority',common/'Xauthority')
office='[6.0]\nwpsoffice\\Application%20Settings\\AppComponentMode=prome_independ\ncommon\\AcceptedEULA=true\n'
(common/'Office.conf').write_text(office)
def fc(path):return '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig><dir>'+path+'/fonts</dir><cachedir>'+path+'/font-cache</cachedir><alias><family>Arial</family><prefer><family>Noto Sans</family></prefer></alias><alias><family>sans-serif</family><prefer><family>Noto Sans</family><family>Noto Sans CJK SC</family></prefer></alias></fontconfig>'
(common/'fonts.conf').write_text(fc(str(common)))
with tarfile.open(P/'common.tar','w') as t:t.add(common,arcname='common')
base='/tmp/unifier-wps-ab-20261103'
with (P/'common.tar').open('rb') as f:subprocess.run(['podman','exec','-i','--user','0',CID,'tar','-x','-C',base],stdin=f,check=True)
subprocess.run(['podman','exec','--user','0',CID,'chown','-R','1000:1000',base+'/common'],check=True)
with subprocess.Popen(['podman','exec','-i','--user','1000:1000',CID,'sh','-c','cat > '+base+'/common/fonts.conf'],stdin=subprocess.PIPE) as c:c.communicate(fc(base+'/common').encode());assert c.returncode==0
hashes={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in common.iterdir() if f.is_file() and f.name!='Xauthority'}
hashes.update({f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (common/'fonts').iterdir()})
bins=['wps','et','wpp','wpsoffice','libkso.so','libet.so','libwpp.so']
native=P/'native-29183/opt/kingsoft/wps-office/office6';actual=[x for x in bins if (native/x).exists()]
nh={x:hashlib.sha256((native/x).read_bytes()).hexdigest() for x in actual}
gh=subprocess.check_output(['podman','exec','--user','0',CID,'sha256sum',*[base+'/root/opt/kingsoft/wps-office/office6/'+x for x in actual]],text=True)
gh={x.split()[-1].rsplit('/',1)[-1]:x.split()[0] for x in gh.splitlines()}
result={'fixture_font_hashes':hashes,'native_binary_hashes':nh,'guest_binary_hashes':gh,'same_binary_content':nh==gh,'profile':office,'native_rpm_version':'12.1.2.29183.AK.preread.sw-1','guest_deb_version':'12.1.2.29183.AK.preread.sw'}
(P/'comparison-inputs.json').write_text(json.dumps(result,indent=2));print(json.dumps({'same_binary_content':nh==gh,'files':actual}),flush=True)
