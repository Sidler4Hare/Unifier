"""Rebind the retained Tencent installation to the accepted current runtime."""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib

RUN = Path(__file__).resolve().parent
CTX = json.loads((RUN / 'context.json').read_text())
ROOT, BACKUP = Path(CTX['root']), Path(CTX['backup'])
APP = Path.home() / '.local/share/multilinux/apps/wemeet'
DESKTOP = Path.home() / '.local/share/applications/wemeetapp.desktop'
sys.path[:0] = [str(APP / 'src'), str(APP)]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def cmd(argv, timeout=120):
    p = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'{argv[:4]}: {p.returncode}: {p.stderr[-2000:]}')
    return p.stdout


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_fingerprint(container, path):
    p = subprocess.Popen(['podman', 'cp', f'{container}:{path}', '-'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = {}
    with tarfile.open(fileobj=p.stdout, mode='r|') as stream:
        for member in stream:
            entry = {'type': member.type.decode('ascii'), 'uid': member.uid, 'gid': member.gid,
                     'mode': member.mode, 'link': member.linkname}
            if member.isfile():
                f = stream.extractfile(member)
                h = hashlib.sha256()
                for data in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(data)
                entry['sha256'] = h.hexdigest()
                entry['size'] = member.size
            result[member.name] = entry
    p.stdout.close()
    error = p.stderr.read().decode()
    assert p.wait() == 0, error
    return result


def main():
    assert json.loads((BACKUP / 'backup-verified.json').read_text())['status'] == 'VERIFIED_BEFORE_ANY_REMOVAL'
    assert not (RUN / 'wemeet-deployment.json').exists()
    old_env = json.loads((APP / '.lab/state/environments/meeting.json').read_text())
    old_launch = json.loads((APP / '.lab/state/launches/wemeet.json').read_text())
    old_name, old_id = old_env['runtime']['container_name'], old_env['runtime']['container_id']
    old_info = json.loads(cmd(['podman', 'inspect', old_id]))[0]
    assert old_info['Name'].lstrip('/') == old_name and old_info['State']['Status'] != 'running'
    checkpoint = next(x for x in json.loads((BACKUP / 'checkpoints.json').read_text()) if x['container_id'] == old_id)
    before_home = tree_fingerprint(old_id, '/home/multilinux-1000')
    before_dpkg = tree_fingerprint(old_id, '/var/lib/dpkg/status')
    save(BACKUP / 'wemeet-home-before.json', before_home)
    save(BACKUP / 'wemeet-dpkg-before.json', before_dpkg)
    documents = Path(old_launch['workdir'])
    before_documents = {str(p.relative_to(documents)): sha(p) for p in documents.rglob('*') if p.is_file() and not p.is_symlink()}
    save(BACKUP / 'wemeet-documents-before.json', before_documents)
    retired = BACKUP / 'retired-wemeet-deployment'
    retired.mkdir(mode=0o700)
    lock = APP / '.lab/leases/meeting.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        for name in ('src', 'tools'):
            shutil.move(APP / name, retired / name)
            shutil.copytree(ROOT / name, APP / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(APP / 'config', retired / 'config')
        shutil.copyfile(ROOT / 'config/targets.toml', APP / 'config/targets.toml')
        image_lock = json.loads((ROOT / 'config/images.lock.json').read_text())
        current_tag = 'localhost/unifier/wemeet-current:' + CTX['stamp']
        cmd(['podman', 'tag', checkpoint['image_id'], current_tag])
        targets = (APP / 'config/targets.toml').read_text()
        targets = targets.replace('source = "' + image_lock['targets']['debian13']['source'] + '"',
                                  'source = "' + current_tag + '"', 1)
        (APP / 'config/targets.toml').write_text(targets)
        old_image = dict(image_lock['targets']['debian13'])
        old_image.update(source=current_tag, manifest_digest=checkpoint['digest'],
                         locked_reference=current_tag.rsplit(':', 1)[0] + '@' + checkpoint['digest'],
                         source_manifest_digest=checkpoint['digest'], os='linux', architecture='amd64',
                         image_id=checkpoint['image_id'], compressed_layer_bytes=None,
                         derivation={'kind':'private-current-application-checkpoint', 'source_container':old_id,
                                     'parent':old_env['base_image'], 'contains_private_app_state':True, 'publish':False})
        image_lock['targets']['debian13'] = old_image
        save(APP / 'config/images.lock.json', image_lock)
        for relative in ('.lab/state/environments/meeting.json', '.lab/state/environments/meeting.packages.txt',
                         '.lab/state/launches/wemeet.json', '.lab/leases/meeting.json'):
            path = APP / relative
            if path.exists():
                target = retired / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(path, target)
        assert DESKTOP.is_file()
        assert sha(DESKTOP) == json.loads((BACKUP / 'host-files-before.json').read_text())[str(DESKTOP)]
        shutil.move(DESKTOP, retired / 'wemeetapp.desktop')
        old_retired_name = 'unifier-retired-meeting-' + CTX['stamp']
        cmd(['podman', 'rename', old_id, old_retired_name])
    finally:
        os.close(fd)
    # Imports deliberately happen after installing the accepted snapshot.
    from multilinux.runtime import PodmanRuntime
    from multilinux.service import EnvironmentService
    from multilinux.control import Control
    from multilinux.file_routes import FileRoutes
    from multilinux.desktop_integrations import DesktopIntegrationManager
    from multilinux.errors import MultilinuxError
    service = EnvironmentService(APP, PodmanRuntime(), tomllib.loads((APP / 'config/targets.toml').read_text()), image_lock)
    cmd(['/usr/bin/python3', APP / 'tools/ml.py', 'env', 'list'])
    devices = [p for p in old_env['runtime'].get('devices', []) if Path(p).exists()]
    assert set(devices) == set(old_env['runtime']['devices']), 'a formerly available device is missing'
    env = service.create('debian13', 'meeting', [str(documents)], 'pasta', desktop=True,
                         user_namespace='keep-id', devices=devices, portals=True,
                         share_label_policy='preserve-host')
    new_id = env['runtime']['container_id']
    new_info = json.loads(cmd(['podman', 'inspect', new_id]))[0]
    assert new_info['HostConfig'].get('Init') is True
    assert new_info['Path'] == '/run/podman-init'
    assert tree_fingerprint(new_id, '/home/multilinux-1000') == before_home
    assert tree_fingerprint(new_id, '/var/lib/dpkg/status') == before_dpkg
    overlay = {k:v for k,v in old_launch['env'].items() if k not in set(old_env['desktop']['env']) | {'HOME','USER','LOGNAME'}}
    launch = service.add_app('meeting','wemeet',old_launch['executable'],old_launch['argv'],
                             workdir=str(documents),env_overlay=overlay,as_host_user=True,
                             on_demand=True,package_origin=old_launch['package_origin'])
    root = Control(APP).add_root(str(documents))
    project = FileRoutes(APP,service).add_project(root['root_uuid'],'meeting',alias='wemeet-documents')
    integration = DesktopIntegrationManager(APP,service).install(project['project_uuid'], app_id='wemeet',
                      name='腾讯会议',icon=str(Path.home()/'.local/share/icons/hicolor/scalable/apps/wemeetapp.svg'),
                      wm_class='wemeetapp', desktop_alias='wemeetapp',menu_only=True)
    cmd(['desktop-file-validate',DESKTOP])
    # An old exact binding must be rejected before starting the new environment.
    stale = dict(launch['environment_binding'])
    stale['container_id'] = old_id
    rejected = False
    try:
        service.start('meeting', expected_binding=stale)
    except MultilinuxError:
        rejected = True
    assert rejected and service.inspect('meeting')['runtime_state'] == 'stopped'
    assert all(sha(documents/rel) == digest for rel,digest in before_documents.items())
    sources = {str(p.relative_to(ROOT)):sha(p) for directory in ('src','tools') for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    assert all(sha(APP/rel) == digest for rel,digest in sources.items())
    freecad = Path.home()/'.local/share/multilinux/apps/freecad'
    assert all(sha(freecad/rel) == digest for rel,digest in sources.items())
    result = {'status':'DEPLOYED_PENDING_GUI_ACCEPTANCE','old_container_id':old_id,'old_retired_name':old_retired_name,
              'new_container_id':new_id,'environment':env['environment_uuid'],'generation':env['generation'],
              'app_instance_uuid':launch['app_instance_uuid'],'project_uuid':project['project_uuid'],
              'integration_uuid':integration['integration_uuid'],'desktop':str(DESKTOP),
              'source_sha256':sources,'private_home_preserved_before_first_launch':True,
              'private_home_entries':len(before_home),'dpkg_database_identical':True,
              'existing_document_files_preserved':len(before_documents),'init':True,
              'stale_binding_rejected_without_start':rejected,'same_current_code_in_freecad_and_wemeet':True,
              'package_origin':old_launch['package_origin'],'shared_directory':str(documents),
              'package_upgrade_performed':False,'private_recovery':str(BACKUP)}
    save(RUN/'wemeet-deployment.json',result)
    save(APP/'deployment-current.json',{'source_sha256':sources,'application':'wemeet','environment_binding':launch['environment_binding'],
                                      'accepted_source':str(ROOT),'deployment_evidence':str(RUN/'wemeet-deployment.json')})
    print(json.dumps({k:result[k] for k in ['status','new_container_id','private_home_entries','init','same_current_code_in_freecad_and_wemeet']},ensure_ascii=False))


if __name__ == '__main__':
    os.umask(0o077)
    main()
