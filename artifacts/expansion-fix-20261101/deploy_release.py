"""Activate a verified bundle, preserving daily data and executable rollback."""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PRIVATE = ROOT / ".lab/agents/expansion-fix-20261101"
BUNDLE = ROOT / "artifacts/unifier-0.0.2.tar.gz"
DIGEST = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
VERSION = "0.0.2"
RELEASE = Path.home() / ".local/share/unifier-code/releases" / (VERSION + "-" + DIGEST[:12])
ENTRY = Path.home() / ".local/bin/unifier"
BACKUP = PRIVATE / "daily-entrypoints-before-final.tar.gz"
RECEIPT = OUT / "deployment.json"


def atomic(path, data, mode):
    fd, name = tempfile.mkstemp(prefix=".unifier-002-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def link(target):
    fd, name = tempfile.mkstemp(prefix=".unifier-002-", dir=ENTRY.parent)
    os.close(fd)
    os.unlink(name)
    os.symlink(target, name)
    os.replace(name, ENTRY)


if "--rollback" in sys.argv:
    receipt = json.loads(RECEIPT.read_text())
    assert ENTRY.is_symlink() and ENTRY.resolve() == RELEASE / "bin/unifier"
    with tarfile.open(BACKUP) as archive:
        payloads = []
        for member in archive.getmembers():
            if member.isfile():
                path = Path.home() / member.name
                expected = receipt["active_file_sha256"][str(path)]
                assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, path
                payloads.append((path, archive.extractfile(member).read(), member.mode))
        for path, payload, mode in payloads:
            atomic(path, payload, mode)
    link(receipt["previous_launcher_target"])
    print("restored previous entrypoints and deployment metadata; user data untouched")
    raise SystemExit(0)

if RECEIPT.exists() or BACKUP.exists():
    raise SystemExit("deployment receipt or backup already exists; inspect before reuse")
assert DIGEST == Path(str(BUNDLE) + ".sha256").read_text().split()[0]
if RELEASE.exists():
    raise SystemExit("release directory already exists; inspect before reuse")
RELEASE.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix=".prepare-unifier-002-", dir=RELEASE.parent) as temporary:
    with tarfile.open(BUNDLE) as archive:
        archive.extractall(temporary, filter="data")
    os.replace(Path(temporary) / "unifier-0.0.2", RELEASE)
sys.path[:0] = [str(RELEASE / "src"), str(RELEASE)]
from tools.check_repo import verify_bundle
from unifier.install import load_catalog
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService
verify_bundle(RELEASE)
assert ENTRY.is_symlink()
previous = os.readlink(ENTRY)
old_release = ENTRY.resolve().parents[1]
environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C", "LANG": "C"}


def version(path):
    return subprocess.check_output([str(path), "--version"], env=environment, text=True).strip()


with tempfile.TemporaryDirectory(prefix="unifier-002-upgrade-") as temporary:
    directory = Path(temporary)
    bindir = directory / "bin"
    bindir.mkdir()
    destination = bindir / "unifier"
    destination.symlink_to(old_release / "bin/unifier")
    sentinel = directory / "userdata"
    sentinel.write_bytes(b"retained business data")
    versions = [version(destination)]
    installer = [sys.executable, str(RELEASE / "tools/install_unifier.py"),
                 "--bin-dir", str(bindir), "--upgrade"]
    subprocess.run(installer, env=environment, check=True, capture_output=True)
    versions.append(version(destination))
    rollback, = bindir.glob("*.rollback")
    os.replace(rollback, destination)
    versions.append(version(destination))
    subprocess.run(installer, env=environment, check=True, capture_output=True)
    versions.append(version(destination))
    destination.unlink()
    assert sentinel.read_bytes() == b"retained business data"
    assert versions == ["Unifier 0.0.1", "Unifier 0.0.2", "Unifier 0.0.1", "Unifier 0.0.2"]
    (OUT / "install-rollback.json").write_text(json.dumps({"passed": True,
        "versions": versions, "data_preserved_after_uninstall": True}, indent=2))

config, lock = load_catalog(RELEASE)
files, roots = {}, {}
for app in ["freecad", "wemeet"]:
    root = Path.home() / ".local/share/multilinux/apps" / app
    roots[app] = root
    service = EnvironmentService(root, PodmanRuntime(), config, lock)
    service.validate_app_binding(app)
    if app == "wemeet":
        text = (Path.home() / ".local/share/applications/wemeetapp.desktop").read_text()
        command = shlex.split(next(line[5:] for line in text.splitlines() if line.startswith("Exec=")))
        def arg(name): return command[command.index(name) + 1]
        service.validate_integration_launch(arg("--integration"), app_id=app,
            project_uuid=arg("--project"), app_instance_uuid=arg("--app-instance"),
            execution_fingerprint=arg("--execution-fingerprint"))
    for name in ["tools/ml.py", "tools/unifier.py", "deployment-current.json"]:
        path = root / name
        assert path.is_file() and not path.is_symlink()
        files[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
with tarfile.open(BACKUP, "w:gz", dereference=False) as archive:
    for path in [*files, ENTRY]:
        archive.add(path, arcname=str(path.relative_to(Path.home())), recursive=False)
with tarfile.open(BACKUP) as archive:
    for path, (data, mode) in files.items():
        assert archive.extractfile(str(path.relative_to(Path.home()))).read() == data
    assert archive.getmember(str(ENTRY.relative_to(Path.home()))).linkname == previous

receipt = {"version": VERSION, "release": str(RELEASE), "bundle_sha256": DIGEST,
           "backup": str(BACKUP), "previous_launcher_target": previous,
           "activated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "entrypoints": {}}
try:
    for app, root in roots.items():
        wrapper = '''#!/usr/bin/env python3
"""Unifier frozen release; the existing application data root is authoritative."""
import os
from pathlib import Path
import sys
APP_ROOT = Path(ROOT_LITERAL)
RELEASE = Path(RELEASE_LITERAL)
environment = dict(os.environ)
environment["UNIFIER_HOME"] = str(APP_ROOT)
environment["PYTHONPATH"] = str(RELEASE / "src") + os.pathsep + str(RELEASE)
os.execve(sys.executable, [sys.executable, str(RELEASE / "tools/unifier.py"), *sys.argv[1:]], environment)
'''.replace("ROOT_LITERAL", repr(str(root))).replace("RELEASE_LITERAL", repr(str(RELEASE)))
        compile(wrapper, str(root / "tools/unifier.py"), "exec")
        for name in ["tools/ml.py", "tools/unifier.py"]:
            path = root / name
            atomic(path, wrapper.encode(), files[path][1])
            result = subprocess.check_output([sys.executable, str(path), "--version"], env=environment, text=True).strip()
            assert result == "Unifier 0.0.2"
            receipt["entrypoints"][str(path)] = result
        path = root / "deployment-current.json"
        metadata = json.loads(files[path][0])
        metadata["active_entrypoint_release"] = {"version": VERSION, "code_root": str(RELEASE),
            "bundle_sha256": DIGEST, "activated_at": receipt["activated_at"], "backup": str(BACKUP)}
        atomic(path, (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode(), files[path][1])
    installed = subprocess.run([sys.executable, str(RELEASE / "tools/install_unifier.py"), "--upgrade"],
                               env=environment, check=True, capture_output=True, text=True)
    assert version(ENTRY) == "Unifier 0.0.2"
    receipt["entrypoints"][str(ENTRY)] = version(ENTRY)
    receipt["launcher_install_output"] = installed.stdout
    receipt["active_file_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    receipt["passed"] = True
except BaseException:
    for path, (data, mode) in files.items():
        atomic(path, data, mode)
    if os.readlink(ENTRY) != previous:
        link(previous)
    raise
RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(receipt, ensure_ascii=False, indent=2))
