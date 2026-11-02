"""Native GLib desktop launch/default/revoke proof in isolated XDG directories."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())
CONTROLLER = r'''
import sys,json,tomllib
from pathlib import Path
import os,signal
project=Path(sys.argv[1]);base=Path(sys.argv[2]);sys.path[:0]=[str(project/'src'),str(project)]
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.desktop import HostDesktop
from multilinux.desktop_integrations import DesktopIntegrationManager
s=EnvironmentService(project,PodmanRuntime(),tomllib.loads((project/'config/targets.toml').read_text()),json.loads((project/'config/images.lock.json').read_text()))
host=HostDesktop(base/'data/applications',base/'data/mime')
m=DesktopIntegrationManager(project,s,host)
request=json.load(sys.stdin);action=request.pop('action')
if action=='revoke-crash':
 import multilinux.desktop_integrations as module
 count=request.pop('after_files');identifier=request.pop('identifier')
 if count==0:
  save=m._save
  def stopped_save(record):
   result=save(record)
   if record['state']=='revoking':os.kill(os.getpid(),signal.SIGKILL)
   return result
  m._save=stopped_save
 else:
  touched=[0]
  def wrap(function):
   def stopped(*args,**kw):
    result=function(*args,**kw);touched[0]+=1
    if touched[0]==count:os.kill(os.getpid(),signal.SIGKILL)
    return result
   return stopped
  module._atomic_write=wrap(module._atomic_write);module._remove=wrap(module._remove)
 m.revoke(identifier);raise RuntimeError('crash boundary not reached')
if action=='app-add':
 result=s.add_app(request['environment'],request['app_id'],'/bin/sh',
   ['/bin/sh','-c','printf "%s\\n" "$@" > "$0"',request['marker']],
   workdir=request['workspace'],as_host_user=True,on_demand=True)
elif action=='app-remove':
 s.remove_app(request['app_id']);result={'removed':True}
elif action=='install': result=m.install(**request)
elif action=='revoke':result=m.revoke(**request)
elif action=='recover':result=m.recover()
elif action=='show':result=m.show(**request)
else:raise RuntimeError(action)
print(json.dumps(result,ensure_ascii=False))
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    assert not a.workdir.exists() and not a.output.exists()
    a.workdir.mkdir(mode=0o700)
    base = a.workdir.resolve()
    fixtures = json.loads((HERE / "fixtures.json").read_text())
    project = Path(fixtures["project"])
    item = fixtures["targets"]["debian13"]
    work = Path(item["workspace"])
    nonce = uuid.uuid4().hex[:8]
    mime, extension, app = "application/x-multilinux-eprobe-" + nonce, "e" + nonce, "eprobe-" + nonce
    env = dict(os.environ, XDG_DATA_HOME=str(base / "data"), XDG_CONFIG_HOME=str(base / "config"),
               XDG_CACHE_HOME=str(base / "cache"), XDG_DATA_DIRS="/usr/local/share:/usr/share", XDG_CONFIG_DIRS="/etc/xdg")
    for d in [base / "data/applications", base / "data/mime/packages", base / "config", base / "cache"]:
        d.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "scope": "native GLib/GIO with isolated XDG files and one owned fixture app",
              "source_sha256": {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (project / "src/multilinux").glob("*.py")}, "steps": []}
    report["app_id"] = app

    def invoke(action, expect_error=False, **params):
        child = subprocess.run([sys.executable, "-c", CONTROLLER, str(project), str(base)],
                               input=json.dumps(dict(action=action, **params)), env=os.environ,
                               text=True, capture_output=True, timeout=25)
        report["steps"].append({"action": action, "exit": child.returncode, "stderr": child.stderr[-3000:]})
        if expect_error:
            assert child.returncode != 0, "expected conservative desktop conflict"
            return {"error": child.stderr}
        assert child.returncode == 0, (action, child.stderr[-3000:])
        return json.loads(child.stdout)

    def default():
        child = subprocess.run([sys.executable, "-c", 'from gi.repository import Gio;import sys,json;'
            'a=Gio.AppInfo.get_default_for_type(sys.argv[1],False);print(json.dumps(a.get_id() if a else None))', mime],
            env=env, text=True, capture_output=True, timeout=10)
        assert child.returncode == 0, child.stderr
        return json.loads(child.stdout)

    def refresh():
        for argv in [["update-mime-database", str(base / "data/mime")],
                     ["update-desktop-database", str(base / "data/applications")]]:
            child = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
            assert child.returncode == 0, child.stderr

    def launch_desktop(path, filename):
        # Resolver files stay isolated. Restore the child's real XDG storage
        # context so rootless Podman addresses the same existing fixture CID.
        values = {key: os.environ.get(key) for key in ["XDG_DATA_HOME", "XDG_CONFIG_HOME",
                  "XDG_CACHE_HOME", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS"]}
        code = ('from gi.repository import Gio;import sys,json;'
                'app=Gio.DesktopAppInfo.new_from_filename(sys.argv[1]);context=Gio.AppLaunchContext();'
                'values=json.loads(sys.argv[3]);'
                '[(context.unsetenv(k) if v is None else context.setenv(k,v)) for k,v in values.items()];'
                'assert app.launch([Gio.File.new_for_path(sys.argv[2])],context)')
        child = subprocess.run([sys.executable, "-c", code, str(path), str(filename), json.dumps(values)],
                               env=env, text=True, capture_output=True, timeout=20)
        report["steps"].append({"action": "native-GIO-launch", "path": str(path), "exit": child.returncode,
                                "stdout": child.stdout[-3000:], "stderr": child.stderr[-3000:]})
        return child

    try:
        original = base / "data/applications/original-eprobe.desktop"
        original.write_text('[Desktop Entry]\nType=Application\nName=Original E probe\nExec=/usr/bin/true\nMimeType=' + mime + ';\n')
        mimeapps = base / "data/applications/mimeapps.list"
        original_bytes = ('# prior user choice\n[Default Applications]\n' + mime + '=original-eprobe.desktop;\n'
                          'text/plain=unrelated-user-choice.desktop;\n\n[Added Associations]\n' + mime + '=original-eprobe.desktop;\n').encode()
        mimeapps.write_bytes(original_bytes)
        (base / "data/mime/packages/original-eprobe.xml").write_text(
            '<?xml version="1.0"?><mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">'
            '<mime-type type="' + mime + '"><glob pattern="*.' + extension + '"/></mime-type></mime-info>')
        refresh()
        assert default() == original.name, default()
        marker, replacement_marker = work / ("desktop-argv-" + nonce), work / ("replacement-app-" + nonce)
        for f in [marker, replacement_marker]:
            f.unlink(missing_ok=True)
        old_app = invoke("app-add", environment=item["environment"]["id"], app_id=app,
                         marker=str(marker), workspace=str(work))
        record = invoke("install", project_identifier="native-debian13", app_id=app,
                        mime_type=mime, extension=extension, desktop_alias="eprobe-entry-" + nonce)
        assert record["state"] == "installed"
        refresh()
        assert default() == record["desktop_id"] + ".desktop", default()
        report["default_changed_to_exported_entry"] = True
        filename = work / ("原生 文件';%value." + extension)
        filename.write_text("native desktop argument data")
        launched = launch_desktop(record["desktop_path"], filename)
        assert launched.returncode == 0, launched.stderr
        end = time.monotonic() + 12
        while not marker.exists() and time.monotonic() < end:
            time.sleep(.05)
        assert marker.read_text() == str(filename) + "\n", marker.read_text()
        report["real_GIO_launch_argv_preserved"] = True
        end = time.monotonic() + 12
        cid = item["environment"]["runtime"]["container_id"]
        while time.monotonic() < end:
            running = json.loads(subprocess.check_output(["podman", "inspect", cid], text=True))[0]["State"]["Running"]
            if not running:
                break
            time.sleep(.05)
        assert not running
        # An existing entry must not silently authorize a same-name new app.
        invoke("app-remove", app_id=app)
        new_app = invoke("app-add", environment=item["environment"]["id"], app_id=app,
                         marker=str(replacement_marker), workspace=str(work))
        assert old_app["app_instance_uuid"] != new_app["app_instance_uuid"]
        launch_desktop(record["desktop_path"], filename)
        time.sleep(1.5)
        assert not replacement_marker.exists(), "stale entry authorized the replacement app instance"
        report["recreated_app_refused_by_old_entry"] = True
        revoked = invoke("revoke", identifier=record["integration_uuid"])
        assert revoked["state"] == "revoked"
        invoke("recover")
        invoke("recover")
        refresh()
        assert not Path(record["desktop_path"]).exists()
        assert mimeapps.read_bytes() == original_bytes and default() == original.name
        assert original.is_file()
        # Cache a fresh entry for the currently valid replacement app, revoke
        # it, then dispatch its old argv from the saved desktop copy.
        record2 = invoke("install", project_identifier="native-debian13", app_id=app,
                         mime_type=mime, extension=extension, desktop_alias="eprobe-cache-" + nonce)
        cached = base / "cached-old-launcher.desktop"
        cached.write_bytes(Path(record2["desktop_path"]).read_bytes())
        revoked2 = invoke("revoke", identifier=record2["integration_uuid"])
        assert revoked2["state"] == "revoked"
        replacement_marker.unlink(missing_ok=True)
        started_at = json.loads(subprocess.check_output(["podman", "inspect", cid], text=True))[0]["State"]["StartedAt"]
        launch_desktop(cached, filename)
        time.sleep(1)
        assert not replacement_marker.exists(), "revoked cached launcher still starts the app"
        state = json.loads(subprocess.check_output(["podman", "inspect", cid], text=True))[0]["State"]
        assert not state["Running"] and state["StartedAt"] == started_at
        assert mimeapps.read_bytes() == original_bytes and default() == original.name
        conflict = invoke("install", project_identifier="native-debian13", app_id=app,
                          mime_type=mime, extension=extension, desktop_alias="eprobe-conflict-" + nonce)
        section, lines, changed = "", [], False
        for line in mimeapps.read_text().splitlines():
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            if section == "Default Applications" and line.startswith(mime + "="):
                line = mime + "=original-eprobe.desktop;"
                changed = True
            lines.append(line)
        assert changed
        foreign_bytes = ("\n".join(lines) + "\n# later user choice\n").encode()
        mimeapps.write_bytes(foreign_bytes)
        assert default() == original.name
        invoke("revoke", identifier=conflict["integration_uuid"], expect_error=True)
        invoke("recover")
        invoke("recover")
        assert mimeapps.read_bytes() == foreign_bytes
        assert Path(conflict["desktop_path"]).is_file()
        assert invoke("show", identifier=conflict["integration_uuid"])["state"] == "conflict"
        report.update(passed=True, original_default_and_exact_bytes_restored=True,
                      unrelated_entries_preserved=True, recovery_twice=True,
                      revoked_cached_launcher_refused_without_start=True,
                      later_external_choice_preserved_on_conflict=True,
                      old_app_instance=old_app["app_instance_uuid"], new_app_instance=new_app["app_instance_uuid"])
    except Exception:
        report["error"] = traceback.format_exc()[-7000:]
    finally:
        with a.output.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"passed": report["passed"], "steps": len(report["steps"]), "output": str(a.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
