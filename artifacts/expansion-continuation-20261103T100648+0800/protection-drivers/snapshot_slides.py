from pathlib import Path
import sys,zipfile,xml.etree.ElementTree as E,json,hashlib
ctl=Path(__file__).resolve().parent;repo=ctl.parents[2];p=repo/'.lab/guarded-expansion-20261103/shared'/sys.argv[2]
with zipfile.ZipFile(p) as z:
 slides=[n for n in z.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')];assert slides==['ppt/slides/slide1.xml'];texts=[n.text for n in E.fromstring(z.read(slides[0])).iter() if n.tag.endswith('}t')]
expected=['中文演示测试','Original line 100','中文、空格和保存重开','UNIFIER-PPT-CYCLE-1 $;']
if sys.argv[3]=='2':expected+=['UNIFIER-PPT-CYCLE-2 $;']
assert texts==expected,texts
out=Path((ctl/'evidence-path').read_text().strip())/'private-gui-workflows-v7';r={'status':'PASS_NATIVE_GUI_SAVED_SLIDE','stage':sys.argv[1],'filename':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'uid':p.stat().st_uid,'texts':texts};(out/(sys.argv[1]+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2));print(r['status'],sys.argv[1])
