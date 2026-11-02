#!/usr/bin/env python3
"""Real UI edit/save/new workflow; requires the registered GUI to be running."""
import hashlib
import json
from pathlib import Path
import sys
import time

HERE=Path(__file__).resolve().parent
ROOT=next(p for p in HERE.parents if (p/"src/multilinux").is_dir())
sys.path.insert(0,str(ROOT))
from gui_command import command
from tools.kwin_window_probe import screenshot


def save(name,data):
    p=HERE/name
    if p.exists():raise RuntimeError('refusing to overwrite '+str(p))
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def wait(predicate):
    deadline=time.monotonic()+12
    last=None
    while time.monotonic()<deadline:
        last=command({'op':'inspect'})
        if predicate(last):return last
        time.sleep(.15)
    raise RuntimeError('GUI condition timeout: '+json.dumps(last,ensure_ascii=False))


def capture(name):
    identity=json.loads((HERE/'edit-window-window.json').read_text())['before']
    for attempt in range(4):
        try:
            result=screenshot(HERE/'observer','',HERE/(name+'.png'),
                              pids=(identity['pid'],),resource_hint='freecad',
                              window_id=identity['internalId'])
            save(name+'-window.json',result)
            return
        except RuntimeError as exc:
            if attempt == 3:raise
            save(name+'-capture-retry-'+str(attempt)+'.json',{'reason':str(exc)})
            time.sleep(.3)


def main():
    work=HERE/'host-project'
    original=work/'宿主原件.FCStd'
    before=sha(original)
    capture('02-opened-model')
    command({'op':'trigger','action':'Part_Box'})
    edited=wait(lambda s:len(s['documents'])==1 and len(s['documents'][0]['objects'])==4)
    save('edited.json',edited)
    command({'op':'view'})
    wait(lambda s:any(a['name']=='Std_Save' and a['enabled'] for a in s['actions']))
    command({'op':'trigger','action':'Std_Save'})
    saved=wait(lambda s:sha(original)!=before and not s['dialogs'])
    save('saved.json',{'gui':saved,'before_sha256':before,'after_sha256':sha(original)})
    capture('03-saved-model')
    command({'op':'trigger','action':'Std_CloseActiveWindow'})
    wait(lambda s:not s['documents'] and not s['dialogs'])
    command({'op':'trigger','action':'Std_Open'})
    dialog=wait(lambda s:any(d['class']=='QFileDialog' for d in s['dialogs']))
    save('open-dialog.json',dialog)
    command({'op':'file_dialog_submit','path':str(original)})
    reopened=wait(lambda s:len(s['documents'])==1 and len(s['documents'][0]['objects'])==4 and not s['dialogs'])
    save('reopened-ui.json',reopened)
    command({'op':'trigger','action':'Std_New'})
    wait(lambda s:len(s['documents'])==2)
    command({'op':'trigger','action':'Part_Box'})
    new=wait(lambda s:any(d['filename']=='' and len(d['objects'])==1 for d in s['documents']))
    save('new-unsaved.json',new)
    command({'op':'view'})
    wait(lambda s:any(a['name']=='Std_Save' and a['enabled'] for a in s['actions']))
    command({'op':'trigger','action':'Std_Save'})
    dialog=wait(lambda s:any(d['class']=='QFileDialog' for d in s['dialogs']))
    save('save-as-dialog.json',dialog)
    file_dialog=[d for d in dialog['dialogs'] if d['class']=='QFileDialog'][0]
    if Path(file_dialog['directory'])!=work:
        raise RuntimeError('Save As default is not the host project; inspect saved dialog evidence')
    # A basename only proves that the dialog's actual default directory works.
    submission=command({'op':'file_dialog_submit','path':'新建 零件.FCStd'})
    save('save-as-submission.json',submission)
    created=wait(lambda s:(work/'新建 零件.FCStd').is_file() and not s['dialogs'])
    save('new-saved.json',created)
    capture('04-new-saved-model')
    save('gui-workflow.json',{'passed':True,'save_as_default_directory':file_dialog['directory'],
         'files':{p.name:{'sha256':sha(p),'uid':p.stat().st_uid,'gid':p.stat().st_gid,'bytes':p.stat().st_size}
                  for p in work.iterdir() if p.is_file()},
         'scope':'Real visible Wayland GUI; QAction and Qt file-dialog automation, no human input; no App.saveAs/openDocument/newDocument in test controller.'})
    print('Real GUI open/edit/save/reopen/new/Save As completed',flush=True)


if __name__=='__main__':main()
