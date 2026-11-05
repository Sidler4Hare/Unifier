import subprocess,os,json,time
from pathlib import Path
P=Path(__file__).resolve().parent
cells=[('native','writer','CWNA'),('guest','writer','CWGA'),('guest','sheet','CSGA'),('native','sheet','CSNA'),('native','slides','CPNA'),('guest','slides','CPGA'),('guest','family','CFGA'),('native','family','CFNA'),('native','family','CFNB'),('guest','family','CFGB')]
if os.environ.get('CORE_RERUN_GUEST')=='1':cells=[('guest','writer','CWGB'),('guest','sheet','CSGB'),('guest','slides','CPGB'),('guest','family','CFGC'),('guest','family','CFGD')]
result={'status':'RUNNING','cells':[]};out=P/'core-batch.json'
out.write_text(json.dumps(result,indent=2))
try:
 for side,case,label in cells:
  assert not (P/'guardian-abort.json').exists(),'guardian abort'
  (P/'batch-heartbeat').write_text(str(time.time()))
  with (P/('cell-'+label+'.log')).open('w') as log:
   r=subprocess.run(['/usr/bin/python3',str(P/'run_cell.py'),side,case,label],stdout=log,stderr=subprocess.STDOUT,timeout=120)
  cell=json.loads((P/'cells'/label/'result.json').read_text());result['cells'].append({'label':label,'status':cell['status'],'summary':cell.get('summary')});out.write_text(json.dumps(result,indent=2))
  assert r.returncode==0 and cell['status']=='MEASURED',(label,cell.get('error'))
 result['status']='COMPLETED_PILOT'
except Exception as e:result.update(status='STOPPED',error=str(e))
finally:out.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2),flush=True)
