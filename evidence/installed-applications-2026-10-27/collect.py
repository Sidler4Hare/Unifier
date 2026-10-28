"""Read installed metadata and associations without launching applications."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import configparser
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from collections import Counter,defaultdict

import rpm
from gi.repository import Gio

RUN=Path(__file__).resolve().parent
ROOT=RUN.parents[1]
HOME=Path.home()
TS=rpm.TransactionSet()


def save(name,data):
    (RUN/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def command(argv):
    p=subprocess.run(list(map(str,argv)),capture_output=True,text=True,timeout=90)
    return {'argv':list(map(str,argv)),'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr}


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def metadata(h):
    out={k:str(h[k] or '') for k in ['name','version','release','arch','license','vendor','packager','url','sourcerpm','summary']}
    out['epoch']=str(h['epoch'] or 0)
    out['nevra']=f"{out['name']}-{out['epoch']}:{out['version']}-{out['release']}.{out['arch']}"
    return out


def owners(path):
    # RPM's Python basenames index expects the complete pathname here.
    return sorted({str(h['name']) for h in TS.dbMatch('basenames',str(path)) if str(path) in h['filenames']})


def file_snapshot(paths):
    result={}
    for p in paths:
        p=Path(p)
        if p.is_file():result[str(p)]={'sha256':sha(p),'symlink':str(p.readlink()) if p.is_symlink() else None}
    return result


MIMES=['application/vnd.openxmlformats-officedocument.wordprocessingml.document',
       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
       'application/vnd.openxmlformats-officedocument.presentationml.presentation',
       'application/pdf','text/plain','text/markdown','text/x-python','text/html','video/mp4',
       'model/step','application/x-extension-fcstd','x-scheme-handler/http','x-scheme-handler/https',
       'x-scheme-handler/weixin','x-scheme-handler/wechat','x-scheme-handler/vscode',
       'x-scheme-handler/qoder-cn','x-scheme-handler/zcode','x-scheme-handler/steam']


def associations():
    result=[]
    for mime in MIMES:
        default=Gio.AppInfo.get_default_for_type(mime,False)
        apps=Gio.AppInfo.get_all_for_type(mime)
        result.append({'mime':mime,'default':{'id':default.get_id(),'name':default.get_name()} if default else None,
                       'handlers':[{'id':a.get_id(),'name':a.get_name()} for a in apps],
                       'default_present_in_handler_enumeration':default.get_id() in {a.get_id() for a in apps} if default else None})
    return result


def main():
    packages=sorted([metadata(h) for h in TS.dbMatch()],key=lambda p:(p['name'],p['arch']))
    save('rpm-installed.json',packages)
    containers=command(['podman','ps','-a','--format','json']);assert containers['exit']==0
    rows=json.loads(containers['stdout'])
    save('containers-before.json',rows)
    roots=[Path(os.environ.get('XDG_DATA_HOME',str(HOME/'.local/share')))]
    roots += [Path(x) for x in os.environ.get('XDG_DATA_DIRS','/usr/local/share:/usr/share').split(':') if x]
    roots=list(dict.fromkeys(roots))
    desktop_paths=[]
    for root in roots:
        directory=root/'applications'
        if directory.exists():desktop_paths += list(directory.rglob('*.desktop'))
    mime_paths=[]
    for root in [HOME/'.config',HOME/'.local/share/applications',Path('/etc/xdg'),Path('/usr/share/applications')]:
        mime_paths += list(root.glob('*mimeapps.list'))
    protected=desktop_paths+mime_paths
    for name in ['freecad','wemeet']:
        p=HOME/'.local/share/multilinux/apps'/name/'.lab/state'
        protected += [x for x in p.rglob('*') if x.is_file()]
    save('registration-state-before.json',file_snapshot(protected))
    before_associations=associations();save('mime-associations.json',before_associations)
    entries=[];parse_errors=[]
    for rank,root in enumerate(roots):
        directory=root/'applications'
        for p in sorted(directory.rglob('*.desktop')) if directory.exists() else []:
            c=configparser.ConfigParser(interpolation=None,strict=False);c.optionxform=str
            try:
                c.read(p,encoding='utf-8');d=c['Desktop Entry']
                if d.get('Type')!='Application':continue
                argv=shlex.split(d.get('Exec',''))
                program=argv[0] if argv else None
                resolved=shutil.which(program) if program and not program.startswith('/') else program
                mimes=[m for m in d.get('MimeType','').split(';') if m]
                entry={'desktop_id':str(p.relative_to(directory)).replace('/','-'),'path':str(p),
                       'xdg_directory_rank':rank,'name':d.get('Name'),'name_zh':d.get('Name[zh_CN]'),
                       'exec':d.get('Exec'),'try_exec':d.get('TryExec'),'argv0':program,
                       'resolved_argv0_in_inspection_context':os.path.realpath(resolved) if resolved else None,
                       'executable_exists':Path(resolved).is_file() if resolved else False,
                       'rpm_owners':owners(p),'hidden':d.get('Hidden','false').lower()=='true',
                       'no_display':d.get('NoDisplay','false').lower()=='true',
                       'dbus_activatable':d.get('DBusActivatable'),'startup_wm_class':d.get('StartupWMClass'),
                       'flatpak_app_id':d.get('X-Flatpak'),'categories':d.get('Categories'),
                       'mime_types':sorted(set(mimes)),'duplicate_mime_declarations':{k:v for k,v in Counter(mimes).items() if v>1},
                       'unifier_app':d.get('X-Multilinux-App'),'unifier_environment':d.get('X-Multilinux-Environment'),
                       'desktop_sha256':sha(p)}
                entries.append(entry)
            except Exception as exc:parse_errors.append({'path':str(p),'error':str(exc)})
    save('desktop-entries.json',{'xdg_roots':list(map(str,roots)),'entries':entries,'parse_errors':parse_errors,
                               'path_resolution_context':'this inspection process, not proof of KOS/interactive shell PATH'})
    effective=[]
    for app in Gio.AppInfo.get_all():
        effective.append({'id':app.get_id(),'name':app.get_name(),
                          'filename':app.get_filename() if hasattr(app,'get_filename') else None,
                          'should_show':app.should_show()})
    save('effective-gio-applications.json',effective)
    flatpak=command(['flatpak','list','--app','--columns=application,name,version,branch,origin,installation'])
    assert flatpak['exit']==0
    flatpak_apps=[]
    for line in flatpak['stdout'].splitlines():
        if not line.strip():continue
        fields=line.split('\t');assert len(fields)==6
        item=dict(zip(['app_id','name','version','branch','origin','installation'],fields))
        info=command(['flatpak','info','--'+item['installation'],'--show-permissions',item['app_id']])
        item['permissions']=info['stdout'] if info['exit']==0 else None
        flatpak_apps.append(item)
    save('flatpak-applications.json',flatpak_apps)
    commands=[]
    for name in ['wps','et','wpp','wpspdf','wechat','code','zcode','qoder-cn','google-chrome','google-chrome-stable',
                 'kwrite','kate','libreoffice','soffice','mpv','haruna','dragon','okular','qwen','dsh','codex','claude','node','npm','python3','git']:
        path=shutil.which(name)
        item={'command':name,'path':path,'resolved':os.path.realpath(path) if path else None,
              'rpm_owners':owners(path) if path else [],'context':'agent inspection process'}
        if path and Path(path).is_file():
            item['sha256']=sha(path)
            if name=='qwen':
                text=Path(path).read_bytes()[:8192].decode(errors='replace')
                item['wrapper_indicators']=[x for x in ['wine','bottles','flatpak','winapps','freerdp','.exe'] if x in text.lower()]
        commands.append(item)
    save('command-resolution.json',commands)
    artifacts=[]
    for base in [HOME/'下载',HOME/'Downloads',HOME/'Applications',HOME/'.local/share/appimages']:
        if not base.exists():continue
        for directory,dirs,files in os.walk(base):
            if len(Path(directory).relative_to(base).parts)>=2:dirs[:]=[]
            for name in sorted(files):
                p=Path(directory)/name
                if p.suffix.lower() not in ['.rpm','.deb','.appimage'] or not p.is_file():continue
                item={'path':str(p),'bytes':p.stat().st_size,'sha256':sha(p),'kind':p.suffix.lower().lstrip('.')}
                if item['kind']=='rpm':
                    q=command(['rpm','-qp','--qf','%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{LICENSE}\t%{EPOCHNUM}\t%{SHA256HEADER}',p])
                    if q['exit']==0:
                        fields=q['stdout'].split('\t');item['package']=dict(zip(['name','version','release','arch','license','epoch'],fields[:6]))
                        item['rpm_immutable_header_sha256']=fields[6]
                        item['installed_exact_nevra']=any(all(x[k]==item['package'][k] for k in ['name','epoch','version','release','arch']) for x in packages)
                        installed=next((x for x in packages if x['name']==item['package']['name']),None)
                        if installed:
                            item['immutable_header_matches_installed']=command(['rpm','-q','--qf','%{SHA256HEADER}',installed['name']])['stdout']==fields[6]
                    else:item['metadata_error']=q['stderr'][-1000:]
                elif item['kind']=='deb':
                    members=subprocess.check_output(['ar','t',str(p)],text=True).splitlines()
                    member=next(x for x in members if x.startswith('control.tar'))
                    data=subprocess.check_output(['ar','p',str(p),member])
                    if member.endswith('.zst'):data=subprocess.check_output(['zstd','-q','-dc'],input=data)
                    with tarfile.open(fileobj=io.BytesIO(data),mode='r:*') as archive:
                        m=next(x for x in archive if x.name.lstrip('./')=='control')
                        text=archive.extractfile(m).read().decode(errors='replace')
                    item['package']={}
                    for line in text.splitlines():
                        if line.startswith(('Package:','Version:','Architecture:','Maintainer:','Homepage:')):
                            k,v=line.split(':',1);item['package'][k.lower()]=v.strip()
                    item['installed_by_host_rpm_not_inferred']=True
                else:
                    head=p.open('rb').read(16);item['elf_header']=head.startswith(b'\x7fELF');item['appimage_magic']=head[8:11].hex()
                    item['status']='LOCAL_ARTIFACT_ONLY_INSTALLATION_NOT_CONFIRMED'
                artifacts.append(item)
    save('local-installers.json',artifacts)
    duplicate_files=defaultdict(list)
    for item in artifacts:duplicate_files[item['sha256']].append(item['path'])
    save('duplicate-installer-content.json',[{'sha256':h,'paths':v} for h,v in duplicate_files.items() if len(v)>1])
    selected_names={'wps-office','wechat','code','zcode','qoder-cn','google-chrome-stable','clash-verge','chatgpt',
                    'grok-bot','marktext','cc-switch','libreoffice-writer','libreoffice-calc','libreoffice-impress',
                    'libreoffice-draw','kwrite','mpv','haruna','dragon','okular'}
    selected=[x for x in packages if x['name'] in selected_names]
    save('selected-installed-packages.json',selected)
    # Grouping evidence, not a product identity-merging rule.
    by_program=defaultdict(list)
    for e in entries:
        if e['resolved_argv0_in_inspection_context']:
            by_program[(e['resolved_argv0_in_inspection_context'],tuple(e['rpm_owners']))].append(e)
    groups=[]
    for (program,owner),items in by_program.items():
        if len(items)>1:
            groups.append({'program':program,'rpm_owners':list(owner),
                           'entries':[{'id':e['desktop_id'],'name':e['name'],'exec':e['exec'],'no_display':e['no_display']} for e in items],
                           'interpretation':'same launch program; component/URI/field-code semantics still distinct'})
    save('shared-launch-program-groups.json',groups)
    save('summary.json',{'status':'INSTALLED_METADATA_AND_ASSOCIATIONS_OBSERVED_GUI_WORKFLOWS_NOT_RUN',
         'recorded_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'rpm_package_count':len(packages),
         'desktop_entry_records':len(entries),'effective_gio_application_records':len(effective),'flatpak_application_count':len(flatpak_apps),
         'local_installer_files':len(artifacts),'same_content_installer_groups':sum(len(v)>1 for v in duplicate_files.values()),
         'selected_rpm_packages':len(selected),'container_count':len(rows),'containers_running':sum(x['State']=='running' for x in rows),
         'not_performed':['application launches','install/uninstall/update','default association changes','container starts','user profile or credentials inspection'],
         'scope':'RPM database, XDG desktop exports, Flatpak application records, bounded local installer directories, selected PATH commands. Not an exhaustive scan of all private directories or unregistered portable software.'})
    print(json.dumps({'rpm_packages':len(packages),'desktop_records':len(entries),'flatpak_apps':len(flatpak_apps),
                      'local_installers':len(artifacts),'selected_installed':[{'name':x['name'],'version':x['version']} for x in selected],
                      'local_rpm_matches':[{'file':Path(x['path']).name,'installed_exact_nevra':x.get('installed_exact_nevra')} for x in artifacts if x['kind']=='rpm']},ensure_ascii=False,indent=2))


if __name__=='__main__':
    os.umask(0o077)
    main()
