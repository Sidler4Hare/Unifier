#!/usr/bin/env python3
"""Offline verification of the contract fix and final real-GUI evidence."""
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]


def read(name):return json.loads((HERE/name).read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(output):
    manifest=read('gui-manifest.json')
    checks={}
    checks['frozen_gui_source_matches_live_product']=all(
        sha(ROOT/p)==h and sha(HERE/'project'/p)==h
        for p,h in manifest['code_sha256'].items() if not p.startswith('config/'))
    checks['frozen_image_config_unchanged']=all(
        sha(HERE/'project'/p)==h for p,h in manifest['code_sha256'].items() if p.startswith('config/'))
    log=(HERE/'unittest.txt').read_text()
    checks['regression_162_tests_4_skipped']='Ran 162 tests' in log and 'OK (skipped=4)' in log and 'FAILED (' not in log
    workflow=read('gui-workflow.json');save=read('saved.json');post=read('postconditions.json')
    checks['gui_workflow_passed']=workflow['passed'] is True
    checks['original_file_actually_changed']=save['before_sha256']!=save['after_sha256']
    checks['input_is_original_evidence_copy']=save['before_sha256']==manifest['input_sha256']
    checks['original_evidence_untouched']=sha(Path(manifest['input_source']))==manifest['input_sha256']
    submission=read('save-as-submission.json')
    checks['save_as_default_host_project']=submission['initial_directory']==str(HERE/'host-project')
    checks['save_as_submitted_basename_only']=submission['submitted_path']=='新建 零件.FCStd'
    checks['save_as_used_real_save_button']=submission['button']=='&Save'
    gui=read('reopened-after-stop.json')
    checks['native_wayland_visible']=gui['platform']=='wayland' and gui['visible']
    checks['gui_uid_matches_host_files']=gui['uid']==1000
    readback=read('host-readback.json')
    for name,count in [('宿主原件.FCStd',4),('新建 零件.FCStd',1)]:
        path=HERE/'host-project'/name
        doc=next(d for d in gui['documents'] if d['filename']==str(path))
        checks[name+'_objects_reloaded']=len(doc['objects'])==count and all(o['valid'] for o in doc['objects'])
        checks[name+'_hash_stable']=sha(path)==workflow['files'][name]['sha256']==readback[name]['sha256']
        checks[name+'_owner']=path.stat().st_uid==1000 and path.stat().st_gid==1000
        with zipfile.ZipFile(path) as archive:
            checks[name+'_zip_crc']=archive.testzip() is None
            checks[name+'_host_xml_objects']=len(ET.fromstring(archive.read('Document.xml')).findall('./Objects/Object'))==count
    cut=next(o for d in gui['documents'] for o in d['objects'] if o['name']=='Cut')
    checks['original_model_geometry_preserved']=abs(cut['volume']-21738.05328941535)<1e-6
    cube=next(o for d in gui['documents'] if d['filename'].endswith('新建 零件.FCStd') for o in d['objects'])
    checks['new_cube_volume']=abs(cube['volume']-1000)<1e-6
    checks['sibling_note_unchanged']=sha(HERE/'host-project/旁注.txt')==hashlib.sha256('宿主项目旁注，不应被保存操作修改。\n'.encode()).hexdigest()
    for name in ('02-opened-model','03-saved-model','04-new-saved-model','06-reopened'):
        entry=read(name+'-window.json')
        checks[name+'_real_window']=all(
            'XdgToplevelWindow' in entry[side]['objectType'] and entry[side]['bufferWidth']>0 and entry[side]['bufferHeight']>0
            for side in ('before','after')) and entry['before']['internalId']==entry['after']['internalId']
        checks[name+'_capture_exists']=Path(entry['path']).is_file()
    checks['stopped_between_gui_sessions']=read('stopped-before-reopen.json')['runtime_state']=='stopped'
    checks['same_environment_after_restart']=read('stopped-before-reopen.json')['runtime']['container_id']==post['environment']['runtime']['container_id']
    checks['final_stopped']=post['environment']['runtime_state']=='stopped'
    checks['existing_environments_unchanged']=post['existing_container_states_unchanged']
    checks['no_windows_or_processes']=not post['windows'] and not post['matching_processes']
    checks['no_leases_or_bridge']=all(not v['leases'] and v['bridge'] is None for v in post['leases'].values())
    checks['normal_launcher_exits']=all(v==0 for v in post['launcher_exit_codes'].values())
    checks['live_arch_plan_and_query']=read('environment-plan.json')['package_driver']=='pacman' and read('freecad-package-query.json')['present']
    result={'checks':checks,'all_passed':all(checks.values()),
            'product_verdict':'bounded-real-gui-workflow-verified' if all(checks.values()) else 'failed',
            'regression':{'run':162,'passed':158,'skipped':4},
            'test_source_sha256':sha(ROOT/'tests/test_catalog.py'),
            'scope':'Fedora KDE host; one Arch FreeCAD 1.1.4 environment; real Wayland windows and Qt action/dialog automation; not human click or general desktop file-routing acceptance.'}
    with output.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n')
    print(json.dumps({'all_passed':result['all_passed'],'failed':[k for k,v in checks.items() if not v],'checks':len(checks)},ensure_ascii=False))
    return 0 if result['all_passed'] else 1


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('usage: verify.py NEW_OUTPUT.json')
    raise SystemExit(verify(Path(sys.argv[1])))
