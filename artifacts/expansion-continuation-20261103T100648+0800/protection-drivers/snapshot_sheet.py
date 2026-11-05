from pathlib import Path
import sys,zipfile,xml.etree.ElementTree as E,json,hashlib
ctl=Path(__file__).resolve().parent;repo=ctl.parents[2];p=repo/'.lab/guarded-expansion-20261103/shared'/sys.argv[2];ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
with zipfile.ZipFile(p) as z:
 strings=[''.join(e.itertext()) for e in E.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si',ns)]
 cells={}
 for c in E.fromstring(z.read('xl/worksheets/sheet1.xml')).findall('.//s:c',ns):
  v=c.findtext('s:v',namespaces=ns);cells[c.attrib['r']]={'value':strings[int(v)] if c.attrib.get('t')=='s' else v,'formula':c.findtext('s:f',namespaces=ns)}
expect1={'A1':'项目','B1':'数量','A3':'中文 样本','A6':'UNIFIER-SHEET-CYCLE-1 $;','B2':'3','D2':'9','D4':'29','B6':'111','D6':'211'}
expect2={**expect1,'A7':'UNIFIER-SHEET-CYCLE-2 $;','B2':'5','D2':'15','D4':'35','B6':'222','D6':'322'}
expected=expect1 if sys.argv[3]=='1' else expect2
assert all(cells[k]['value']==v for k,v in expected.items()),cells
assert cells['D2']['formula']=='B2*C2' and cells['D4']['formula']=='SUM(D2:D3)' and cells['D6']['formula']=='SUM(100,B6)'
r={'status':'PASS_NATIVE_GUI_SAVED_CELLS','stage':sys.argv[1],'filename':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'uid':p.stat().st_uid,'cells':cells}
out=Path((ctl/'evidence-path').read_text().strip())/'private-gui-workflows-v6';(out/(sys.argv[1]+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2));print(r['status'],sys.argv[1])
