"""Install a persistent user-level FreeCAD menu entry using accepted B code.

Owns only a new support directory, a new CAD project, and its own desktop ID.
Uses a preinstalled local immutable Arch image; no host package installation.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
APP = Path.home() / '.local/share/multilinux/apps/freecad'
WORK = Path.home() / '文档/Unifier-CAD'


def save(name, data):
    with (HERE / name).open('x') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    if APP.exists() or WORK.exists():
        raise RuntimeError('refusing to replace an existing daily application/project')
    before = {str(p): digest(p) for p in (Path.home() / '.local/share/applications').glob('*.desktop')}
    save('desktop-before.json', before)
    APP.mkdir(mode=0o700, parents=True)
    WORK.mkdir(mode=0o700)
    # B's accepted native-GUI snapshot is immutable while dsh develops C.
    source = REPO / 'evidence/unifier-v2-stage-b-2026-10-24/live-freecad/project'
    for folder in ('src', 'tools', 'config'):
        shutil.copytree(source / folder, APP / folder, ignore=shutil.ignore_patterns('__pycache__'))
    sources = {str(p.relative_to(APP)): digest(p) for folder in ('src', 'tools', 'config')
               for p in (APP / folder).rglob('*') if p.is_file()}
    sys.path[:0] = [str(APP / 'src'), str(APP)]
    from multilinux.runtime import PodmanRuntime
    from multilinux.service import EnvironmentService
    from multilinux.file_routes import FileRoutes
    from multilinux.registry import DataRootRegistry
    cfg = tomllib.loads((APP / 'config/targets.toml').read_text())
    lock = json.loads((APP / 'config/images.lock.json').read_text())
    image = lock['targets']['arch']['locked_reference']
    info = json.loads(subprocess.check_output(['podman', 'image', 'inspect', image], text=True))[0]
    assert info['Digest'] == lock['targets']['arch']['manifest_digest']
    service = EnvironmentService(APP, PodmanRuntime(), cfg, lock)
    record = service.create('arch', 'freecad-arch', [str(WORK)], desktop=True,
                            user_namespace='keep-id', gpu=True,
                            share_label_policy='preserve-host')
    save('environment-created.json', record)
    launch = service.add_app('freecad-arch', 'freecad', '/usr/bin/FreeCAD', ['/usr/bin/FreeCAD'],
                             workdir=str(WORK), as_host_user=True, on_demand=True,
                             env_overlay={'QT_QPA_PLATFORM': 'wayland', 'LC_ALL': 'C.UTF-8'})
    save('application-created.json', launch)
    package = service.query_package('freecad-arch', 'freecad')
    assert package['present']
    save('package.json', package)
    # The icon is copied by Podman's supported cp interface from our stopped CID.
    icon = APP / 'freecad.svg'
    icon_result = subprocess.run(['podman', 'cp', record['runtime']['container_id'] +
                                  ':/usr/share/icons/hicolor/scalable/apps/org.freecad.FreeCAD.svg',
                                  str(icon)], capture_output=True, text=True)
    if icon_result.returncode:
        icon_value = 'applications-engineering'
    else:
        icon_value = str(icon)
    root = DataRootRegistry(APP).add(str(WORK), management_domain='host-project')
    routes = FileRoutes(APP, service)
    handler = routes.add_handler('freecad', intent='edit', save_capability='explicit-path', alias='freecad-edit')
    project = routes.add_project(root['root_uuid'], 'freecad-arch', alias='cad',
                                 intent_handlers={'edit': handler['handler_uuid']})
    exported = service.export_app('freecad', menu_only=True, display_name='FreeCAD',
                                  icon=icon_value, wm_class='org.freecad.FreeCAD',
                                  desktop_alias='unifier-freecad')
    desktop = Path(exported['export']['desktop_path'])
    assert desktop.is_file() and not service.inspect('freecad-arch')['runtime_state'] == 'running'
    after = {str(p): digest(p) for p in (Path.home() / '.local/share/applications').glob('*.desktop')}
    assert {p: h for p, h in after.items() if p in before} == before
    save('deployment.json', {'app_root': str(APP), 'work': str(WORK), 'source_sha256': sources,
          'base': 'accepted Stage B native GUI snapshot', 'image': image, 'image_id': info['Id'],
          'environment': record, 'application': exported, 'root': root, 'handler': handler,
          'project': project, 'desktop': str(desktop), 'desktop_sha256': digest(desktop),
          'icon': icon_value, 'icon_copy_exit': icon_result.returncode,
          'existing_desktop_preserved': True, 'menu_only': True, 'mime_defaults_changed': False})
    print(json.dumps({'desktop': str(desktop), 'project': str(WORK),
                      'environment': 'freecad-arch', 'persistent': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
