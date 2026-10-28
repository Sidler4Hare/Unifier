"""Exercise the actual installed menu, native window, shared lease and stop."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import uuid

RUN=Path(__file__).resolve().parent


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def close_owned(window,directory,hint):
    from gi.repository import Gio,GLib
    plugin='unifier-consolidation-close-'+uuid.uuid4().hex
    script=directory/(plugin+'.js')
    script.write_text('for (const w of workspace.windowList()) { if (w.pid === '+str(window['pid'])+
        ' && String(w.internalId) === '+json.dumps(window['internalId'])+
        ' && String(w.resourceClass).toLowerCase().indexOf('+json.dumps(hint)+') !== -1) { w.closeWindow(); } }')
    bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
    sid=bus.call_sync('org.kde.KWin','/Scripting','org.kde.kwin.Scripting','loadScript',
        GLib.Variant('(ss)',(str(script),plugin)),GLib.VariantType.new('(i)'),Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
    assert sid>=0
    try:
        bus.call_sync('org.kde.KWin',f'/Scripting/Script{sid}','org.kde.kwin.Script','run',None,None,Gio.DBusCallFlags.NONE,5000,None)
    finally:
        time.sleep(.2)
        bus.call_sync('org.kde.KWin','/Scripting','org.kde.kwin.Scripting','unloadScript',GLib.Variant('(s)',(plugin,)),None,Gio.DBusCallFlags.NONE,5000,None)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--app',choices=['wemeet','freecad'],required=True)
    parser.add_argument('--attempt',type=int,default=1)
    args=parser.parse_args()
    app=args.app; env_id='meeting' if app=='wemeet' else 'freecad-arch'
    desktop='wemeetapp' if app=='wemeet' else 'unifier-freecad'
    hint='wemeet' if app=='wemeet' else 'freecad'
    app_root=Path.home()/'.local/share/multilinux/apps'/app
    sys.path[:0]=[str(app_root/'src'),str(app_root)]
    import gi
    from gi.repository import Gio
    from tools.kwin_window_probe import window_info,screenshot
    from multilinux.service import EnvironmentService
    from multilinux.runtime import PodmanRuntime
    from multilinux.project_sessions import ProjectSessions
    service=EnvironmentService(app_root,PodmanRuntime(),tomllib.loads((app_root/'config/targets.toml').read_text()),
                               json.loads((app_root/'config/images.lock.json').read_text()))
    cid=service.store.load(env_id)['runtime']['container_id']
    directory=RUN/(app+'-gui'+('' if args.attempt==1 else '-attempt'+str(args.attempt)));directory.mkdir(exist_ok=False)
    shutil.copyfile(__file__,directory/'driver.py')
    result={'status':'RUNNING','container_id':cid,'cycles':[],
            'scope':'Real menu/window/normal WM close, shared data and lifecycle; no login, meeting or camera acceptance.'}
    result_file=directory/'result.json';save(result_file,result)
    lease_path=app_root/'.lab/leases'/(env_id+'.json')
    cached=Gio.DesktopAppInfo.new(desktop+'.desktop');assert cached
    fixture=None
    for cycle in (1,2):
        assert service.inspect(env_id)['runtime_state']=='stopped'
        start=time.monotonic()
        with (directory/f'launch-{cycle}.log').open('w') as log:
            p=subprocess.run(['gtk-launch',desktop],stdout=log,stderr=log,timeout=15)
        assert p.returncode==0
        stable_id=None;stable_since=start;window=None;pids=[]
        while time.monotonic()-start<80:
            top=subprocess.run(['podman','top',cid,'hpid','comm'],capture_output=True,text=True)
            pids=[int(l.split()[0]) for l in top.stdout.splitlines()[1:] if l.split() and l.split()[0].isdigit()]
            windows=window_info(directory/'observer','',pids=pids,resource_hint=hint) if pids else []
            windows=[w for w in windows if w['width']>300 and w['height']>200 and w['bufferWidth']>0 and w['bufferHeight']>0]
            if windows:
                window=max(windows,key=lambda w:w['width']*w['height'])
                if window['internalId']!=stable_id:
                    stable_id=window['internalId'];stable_since=time.monotonic()
                if time.monotonic()-stable_since>=3 and time.monotonic()-start>=(10 if app=='wemeet' else 18):break
            time.sleep(.3)
        assert window is not None,'no stable application window'
        shot=screenshot(directory/'observer','',directory/f'launch-{cycle}.png',pids=[window['pid']],resource_hint=hint,window_id=window['internalId'])
        leases=json.loads(lease_path.read_text());assert any(v.get('app_id')==app for v in leases['leases'].values())
        item={'cycle':cycle,'window':window,'screenshot':shot,'leases_during':leases,'same_container':cid==service.store.load(env_id)['runtime']['container_id']}
        if app=='wemeet':
            deploy=json.loads((RUN/'wemeet-deployment.json').read_text())
            docs=Path(deploy['shared_directory'])
            fixture=docs/('.unifier-migration-'+json.loads((RUN/'context.json').read_text())['stamp']+'.txt')
            text='Unifier 腾讯会议迁移：共享目录原子保存与停机重开\n'
            if cycle==1:
                sessions=ProjectSessions(app_root,service)
                before={s['session_uuid'] for s in sessions.store.list()}
                command=['/usr/bin/python3',str(app_root/'tools/ml.py'),'project','run',deploy['project_uuid'],'--',
                         '/bin/sh','-c','printf "%s" "$1" > "$2.part" && mv "$2.part" "$2" && printf SHARED_READY && exec sleep 120',
                         'migration-fixture',text,str(fixture)]
                with (directory/'shared-session.log').open('w') as log:
                    worker=subprocess.Popen(command,stdout=log,stderr=log)
                deadline=time.monotonic()+25;session=None
                while time.monotonic()<deadline:
                    found=[s for s in sessions.store.list() if s['session_uuid'] not in before and s['state']=='running' and s.get('guest_state')=='alive']
                    both=json.loads(lease_path.read_text())
                    if found and len(both['leases'])==2 and fixture.exists() and fixture.read_text()==text:
                        session=found[0];break
                    time.sleep(.1)
                assert session is not None,'shared app/project lease did not become ready'
                assert session['environment_binding']['container_id']==cid
                cancel=subprocess.run(['/usr/bin/python3',str(app_root/'tools/ml.py'),'project','session','cancel',session['session_uuid']],capture_output=True,text=True,timeout=25)
                assert cancel.returncode==0,cancel.stderr
                code=worker.wait(timeout=20)
                remaining=json.loads(lease_path.read_text())
                assert code==143 and len(remaining['leases'])==1
                assert service.inspect(env_id)['runtime_state']=='running'
                assert window_info(directory/'observer','',pids=[window['pid']],resource_hint=hint,window_id=window['internalId'])
                item['shared_session']={'session_uuid':session['session_uuid'],'actual_guest_pid':session['guest_pid'],
                        'same_container':True,'leases_during':both,'cancel_exit':cancel.returncode,'session_exit':code,
                        'application_survived_targeted_cancel':True}
                item['host_writeback']={'sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'uid':fixture.stat().st_uid}
                assert fixture.stat().st_uid==os.getuid()
            else:
                assert fixture.read_text()==text
                read=subprocess.run(['/usr/bin/python3',str(app_root/'tools/ml.py'),'project','run',deploy['project_uuid'],'--','/bin/cat',str(fixture)],capture_output=True,text=True,timeout=25)
                assert read.returncode==0 and read.stdout==text,(read.returncode,read.stderr)
                item['shared_file_reopened_after_stop']=True
        close_owned(window,directory/'observer',hint)
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            state=service.inspect(env_id)['runtime_state']
            remaining=json.loads(lease_path.read_text())
            if state=='stopped' and not remaining['leases']:break
            time.sleep(.2)
        item.update(state_after_close=state,leases_after=remaining)
        result['cycles'].append(item);save(result_file,result)
        assert state=='stopped' and not remaining['leases'],'normal window close did not release the last lease and stop'
        print(json.dumps({'app':app,'cycle':cycle,'native_window':True,'normal_close_stopped':True}),flush=True)
    result['status']='PASSED_MENU_REOPEN_AND_LIFECYCLE'
    if fixture:
        result['synthetic_shared_file']=str(fixture)
    save(result_file,result)
    save(RUN/(app+'-gui-acceptance.json'),{'status':result['status'],'result':str(result_file),'cycles':len(result['cycles'])})


if __name__=='__main__':
    os.umask(0o077)
    main()
