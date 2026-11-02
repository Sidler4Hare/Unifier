#!/usr/bin/env python3
import argparse,json,sys,time,traceback
from pathlib import Path
from boundary_cases import CASES,watcher
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'evidence/projection-objects-2026-10-26'))
from real_fuse_probe import Fixture

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    r={'source':str(a.source.resolve()),'started_at':time.time(),'cases':[]};f=None
    def save():(a.out/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
    try:
        f=Fixture(a.source.resolve(),a.out);r['initial_status']=f.initial
        for name,call in CASES:
            try:case={'name':name,'passed':True,'data':call(f)}
            except Exception:case={'name':name,'passed':False,'error':traceback.format_exc()}
            r['cases'].append(case);save();print(json.dumps({'case':name,'passed':case['passed']}),flush=True)
        r['known_open_gate_external_watcher']=watcher(f)
        r['final_status']=f.status()
    except Exception:r['fatal']=traceback.format_exc()
    finally:
        if f:f.close();r['cleanup']=f.cleanup
        r['finished_at']=time.time()
        r['passed']=(len(r['cases'])==len(CASES) and all(c['passed'] for c in r['cases']) and
                     r.get('cleanup',{}).get('normal_unmount',False) and not r.get('fatal'))
        save()
    return 0 if r['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
