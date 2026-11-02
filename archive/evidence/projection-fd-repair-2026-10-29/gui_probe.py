#!/usr/bin/env python3
"""One owned Dolphin window against an isolated projection; normal close only."""
from pathlib import Path
import json,subprocess,sys,time,traceback
ROOT=Path(__file__).resolve().parents[2];RUN=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'evidence/projection-objects-2026-10-26'),str(ROOT/'evidence/whitelist-daily-deploy-2026-10-28')]
from real_fuse_probe import Fixture
from kos_windows import window_info,screenshot,close_owned
out=RUN/'gui-final-3';out.mkdir(exist_ok=False)
r={'scope':'Dolphin real folder view on current Fedora KDE/KOS; inherited worker limit'};f=None;window=None
try:
    f=Fixture(ROOT/'src',out)
    marker='Unifier-FD-repair-1000'
    directory=f.back/marker;directory.mkdir()
    for i in range(1000):(directory/f'document-{i:04d}.txt').write_bytes(b'FD repair acceptance\n')
    log=(out/'dolphin.log').open('w')
    launched=subprocess.Popen(['dolphin','--new-window',str(f.view/marker)],stdout=log,stderr=log)
    r['launcher_pid']=launched.pid
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        matches=window_info(out,marker)
        if len(matches)==1:
            window=matches[0]
            if window['width']>300 and window['bufferWidth']>0:break
        time.sleep(.2)
    assert window is not None,'no owned file manager window'
    deadline=time.monotonic()+60
    while True:
        counters=f.status()['counters']
        if counters['dir_handles']==0 and counters['attr_versions']>=1001:
            break
        assert time.monotonic()<deadline,'file manager enumeration did not complete'
        time.sleep(.3)
    time.sleep(1)
    r['window']=window
    for attempt in range(3):
        shot=screenshot(out,marker,out/'dolphin.png',window_id=window['internalId'])
        if shot['focus_retained_after_capture']:
            r['screenshot']=shot
            break
        (out/'dolphin.png').unlink(missing_ok=True)
    assert 'screenshot' in r,'focus changed during each capture; no visual acceptance'
    r['status']=f.status()
    close_owned(window,out,'dolphin');window=None
    r['launch_exit_after_close']=launched.wait(timeout=15)
    log.close()
    deadline=time.monotonic()+10
    while f.status()['counters']['handles']:
        assert time.monotonic()<deadline,'file manager handles did not drain'
        time.sleep(.1)
    r['after_normal_close']=f.status()
    r['passed']=True
except Exception:r['passed']=False;r['error']=traceback.format_exc()
finally:
    if window:
        try:close_owned(window,out,'dolphin')
        except Exception:r['window_close_error']=traceback.format_exc()
    if f:
        try:f.close();r['cleanup']=f.cleanup
        except Exception:r['passed']=False;r['cleanup_error']=traceback.format_exc()
    (out/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
print(json.dumps({'passed':r['passed'],'screenshot':r.get('screenshot',{}).get('path')}))
raise SystemExit(0 if r['passed'] else 1)
