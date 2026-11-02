"""Verify inventory relationships and that live registrations were not changed."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import hashlib
import json
import subprocess
import rpm
from gi.repository import Gio

RUN=Path(__file__).resolve().parent
ROOT=RUN.parents[1]


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    sources=json.loads((RUN/'source-before.json').read_text())
    registrations=json.loads((RUN/'registration-state-before.json').read_text())
    changed_sources=[p for p,h in sources.items() if not (ROOT/p).is_file() or sha(ROOT/p)!=h]
    changed_registrations=[]
    for p,item in registrations.items():
        f=Path(p)
        if not f.is_file() or sha(f)!=item['sha256'] or (str(f.readlink()) if f.is_symlink() else None)!=item['symlink']:
            changed_registrations.append(p)
    old=json.loads((RUN/'containers-before.json').read_text())
    rows=json.loads(subprocess.run(['podman','ps','-a','--format','json'],check=True,capture_output=True,text=True).stdout)
    keys=['Id','Names','ImageID','State','StartedAt','ExitedAt','ExitCode','Labels','Mounts']
    stable=lambda rs:{r['Id']:{k:r.get(k) for k in keys} for r in rs}
    entries=json.loads((RUN/'desktop-entries.json').read_text())
    e={x['desktop_id']:x for x in entries['entries']}
    chrome=[e[x] for x in ['google-chrome.desktop','com.google.Chrome.desktop']]
    wps=[e[x] for x in ['wps-office-prometheus.desktop','wps-office-wps.desktop']]
    wps_artifact=json.loads((RUN/'wps-reuse-reference.json').read_text())['local_artifact']
    default_changes=[]
    for item in json.loads((RUN/'mime-associations.json').read_text()):
        now=Gio.AppInfo.get_default_for_type(item['mime'],False)
        expected=item['default']['id'] if item['default'] else None
        if (now.get_id() if now else None)!=expected:default_changes.append(item['mime'])
    pkgs=json.loads((RUN/'rpm-installed.json').read_text())
    identity=lambda x:(str(x['name'] or ''),str(x['epoch'] or 0),str(x['version'] or ''),str(x['release'] or ''),str(x['arch'] or ''))
    rpm_now=sorted(identity(h) for h in rpm.TransactionSet().dbMatch())
    rpm_before=sorted(identity(x) for x in pkgs)
    flatpak=subprocess.run(['flatpak','list','--app','--columns=application,name,version,branch,origin,installation'],capture_output=True,text=True,check=True)
    current_fp=sorted(tuple(line.split('\t')) for line in flatpak.stdout.splitlines() if line.strip())
    initial_fp=sorted(tuple(x[k] for k in ['app_id','name','version','branch','origin','installation']) for x in json.loads((RUN/'flatpak-applications.json').read_text()))
    plan=json.loads((RUN/'relationship-plan.json').read_text())
    original=json.loads((ROOT/'evidence/extension-test-plan-2026-10-27/plan.json').read_text())
    checks={
        'product_source_and_test_files_unchanged':not changed_sources,
        'desktop_mime_and_daily_state_files_unchanged':not changed_registrations,
        'both_container_instances_and_start_times_unchanged':stable(old)==stable(rows) and len(rows)==2,
        'rpm_package_identities_unchanged':rpm_before==rpm_now,
        'flatpak_application_records_unchanged':current_fp==initial_fp,
        'queried_default_associations_unchanged':not default_changes,
        'chrome_two_entries_same_rpm_and_exec':all(x['rpm_owners']==['google-chrome-stable'] for x in chrome) and len({x['exec'] for x in chrome})==1,
        'chrome_hidden_and_visible_entries_distinct':sorted(x['no_display'] for x in chrome)==[False,True],
        'wps_umbrella_and_writer_same_program_and_package':all(x['rpm_owners']==['wps-office'] for x in wps) and len({x['argv0'] for x in wps})==1,
        'wps_local_rpm_matches_installed_build':wps_artifact['installed_exact_nevra'] and wps_artifact['immutable_header_matches_installed'],
        'local_wps_file_hash_still_matches':sha(wps_artifact['path'])==wps_artifact['sha256'],
        'desktop_metadata_parse_errors_absent':not entries['parse_errors'],
        'relationship_families_18_unique':len(plan['relationship_families'])==18==len({x['id'] for x in plan['relationship_families']}),
        'relationship_workflows_all_not_run':all(x['workflow_status']=='NOT_RUN' for x in plan['relationship_families']),
        'original_9_installation_cells_retained_not_run':len(original['primary_installation_cells'])==9 and all(x['status']=='NOT_RUN' for x in original['primary_installation_cells']),
    }
    result={'status':'READ_ONLY_INVENTORY_AND_PLAN_VERIFIED' if all(checks.values()) else 'REVIEW_REQUIRED',
            'recorded_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'checks':checks,
            'protected_source_files':len(sources),'protected_registration_or_state_files':len(registrations),
            'changed_product_files':changed_sources,'changed_desktop_or_state_files':changed_registrations,
            'changed_default_queries':default_changes,'containers':len(rows),'running_containers':sum(r['State']=='running' for r in rows),
            'intended_edits_only':['README.md','HANDOFF.md','设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md','evidence/extension-test-plan-2026-10-27/plan.json','new evidence/installed-applications-2026-10-27/ artifacts'],
            'scope':'Metadata observations and planning only; no installed application was launched, installed, updated, removed or assigned a new default by this task.'}
    (RUN/'preservation-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    assert all(checks.values())


if __name__=='__main__':main()
